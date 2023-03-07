import asyncio
import concurrent.futures
import datetime
import gzip
import typing
from collections import deque
from typing import Deque, AsyncIterator, Union, List, Optional, Dict, Callable

import ydb
from .topic_writer import (
    PublicWriterSettings,
    WriterSettings,
    PublicMessage,
    PublicWriterInitInfo,
    InternalMessage,
    TopicWriterStopped,
    TopicWriterError,
    messages_to_proto_requests,
    PublicWriteResultTypes,
    MessageType,
)
from .. import (
    _apis,
    issues,
    check_retriable_error,
    RetrySettings,
)
from .._grpc.grpcwrapper.ydb_topic_public_types import PublicCodec
from .._topic_common.common import (
    TokenGetterFuncType,
)
from .._grpc.grpcwrapper.ydb_topic import (
    UpdateTokenResponse,
    StreamWriteMessage,
    WriterMessagesFromServerToClient,
)
from .._grpc.grpcwrapper.common_utils import (
    IGrpcWrapperAsyncIO,
    SupportedDriverType,
    GrpcWrapperAsyncIO,
)


class WriterAsyncIO:
    _loop: asyncio.AbstractEventLoop
    _reconnector: "WriterAsyncIOReconnector"
    _closed: bool
    _compressor_thread_pool: concurrent.futures.Executor

    @property
    def last_seqno(self) -> int:
        raise NotImplementedError()

    def __init__(self, driver: SupportedDriverType, settings: PublicWriterSettings):
        self._loop = asyncio.get_running_loop()
        self._closed = False
        self._reconnector = WriterAsyncIOReconnector(
            driver=driver, settings=WriterSettings(settings)
        )

    async def __aenter__(self) -> "WriterAsyncIO":
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        await self.close()

    def __del__(self):
        if self._closed or self._loop.is_closed():
            return

        self._loop.call_soon(self.close)

    async def close(self, *, flush: bool = True):
        if self._closed:
            return

        self._closed = True

        await self._reconnector.close(flush)

    async def write_with_ack(
        self,
        messages: Union[MessageType, List[MessageType]],
    ) -> Union[PublicWriteResultTypes, List[PublicWriteResultTypes]]:
        """
        IT IS SLOWLY WAY. IT IS BAD CHOISE IN MOST CASES.
        It is recommended to use write with optionally flush or write_with_ack_futures and receive acks by wait futures.

        send one or number of messages to server and wait acks.

        For wait with timeout use asyncio.wait_for.
        """
        futures = await self.write_with_ack_future(messages)
        if not isinstance(futures, list):
            futures = [futures]

        await asyncio.wait(futures)
        results = [f.result() for f in futures]

        return results if isinstance(messages, list) else results[0]

    async def write_with_ack_future(
        self,
        messages: Union[MessageType, List[MessageType]],
    ) -> Union[asyncio.Future, List[asyncio.Future]]:
        """
        send one or number of messages to server.
        return feature, which can be waited for check send result.

        Usually it is fast method, but can wait if internal buffer is full.

        For wait with timeout use asyncio.wait_for.
        """
        input_single_message = not isinstance(messages, list)
        if isinstance(messages, list):
            for index, m in enumerate(messages):
                messages[index] = PublicMessage._create_message(m)
        else:
            messages = [PublicMessage._create_message(messages)]

        futures = await self._reconnector.write_with_ack_future(messages)
        if input_single_message:
            return futures[0]
        else:
            return futures

    async def write(
        self,
        messages: Union[MessageType, List[MessageType]],
    ):
        """
        send one or number of messages to server.
        it put message to internal buffer

        For wait with timeout use asyncio.wait_for.
        """
        await self.write_with_ack_future(messages)

    async def flush(self):
        """
        Force send all messages from internal buffer and wait acks from server for all
        messages.

        For wait with timeout use asyncio.wait_for.
        """
        return await self._reconnector.flush()

    async def wait_init(self) -> PublicWriterInitInfo:
        """
        wait while real connection will be established to server.

        For wait with timeout use asyncio.wait_for()
        """
        return await self._reconnector.wait_init()


class WriterAsyncIOReconnector:
    _closed: bool
    _loop: asyncio.AbstractEventLoop
    _credentials: Union[ydb.credentials.Credentials, None]
    _driver: ydb.aio.Driver
    _update_token_interval: int
    _token_get_function: TokenGetterFuncType
    _init_message: StreamWriteMessage.InitRequest
    _init_info: asyncio.Future
    _stream_connected: asyncio.Event
    _settings: WriterSettings
    _codec: PublicCodec
    _codec_functions: Dict[PublicCodec, Callable[[bytes], bytes]]
    _encode_executor: Optional[concurrent.futures.Executor]
    _codec_selector_batch_num: int
    _codec_selector_last_codec: Optional[PublicCodec]
    _codec_selector_check_batches_interval: int

    _last_known_seq_no: int
    if typing.TYPE_CHECKING:
        _messages_for_encode: asyncio.Queue[List[InternalMessage]]
    else:
        _messages_for_encode: asyncio.Queue
    _messages: Deque[InternalMessage]
    _messages_future: Deque[asyncio.Future]
    _new_messages: asyncio.Queue
    _stop_reason: asyncio.Future
    _background_tasks: List[asyncio.Task]

    def __init__(self, driver: SupportedDriverType, settings: WriterSettings):
        self._closed = False
        self._loop = asyncio.get_running_loop()
        self._driver = driver
        self._credentials = driver._credentials
        self._init_message = settings.create_init_request()
        self._new_messages = asyncio.Queue()
        self._init_info = self._loop.create_future()
        self._stream_connected = asyncio.Event()
        self._settings = settings

        self._codec_functions = {
            PublicCodec.RAW: lambda data: data,
            PublicCodec.GZIP: gzip.compress,
        }

        if settings.encoders:
            for codec, encoder in settings.encoders.items():
                self._codec_functions[codec] = encoder

        self._encode_executor = settings.encoder_executor

        self._codec_selector_batch_num = 0
        self._codec_selector_last_codec = None
        self._codec_selector_check_batches_interval = 10000

        self._codec = self._settings.codec
        if self._codec and self._codec not in self._codec_functions:
            known_codecs = [key for key in self._codec_functions]
            known_codecs.sort()
            raise ValueError(
                "Unknown codec for writer: %s, supported codecs: %s"
                % (self._codec, known_codecs)
            )

        self._last_known_seq_no = 0
        self._messages_for_encode = asyncio.Queue()
        self._messages = deque()
        self._messages_future = deque()
        self._new_messages = asyncio.Queue()
        self._stop_reason = self._loop.create_future()
        self._background_tasks = [
            asyncio.create_task(self._connection_loop(), name="connection_loop"),
            asyncio.create_task(self._encode_loop(), name="encode_loop"),
        ]

    async def close(self, flush: bool):
        if self._closed:
            return

        if flush:
            await self.flush()

        self._closed = True
        self._stop(TopicWriterStopped())

        background_tasks = self._background_tasks

        for task in background_tasks:
            task.cancel()

        await asyncio.wait(self._background_tasks)

        # if work was stopped before close by error - raise the error
        try:
            self._check_stop()
        except TopicWriterStopped:
            pass

    async def wait_init(self) -> PublicWriterInitInfo:
        done, _ = await asyncio.wait(
            [self._init_info, self._stop_reason], return_when=asyncio.FIRST_COMPLETED
        )
        res = done.pop()  # type: asyncio.Future
        res_val = res.result()

        if isinstance(res_val, BaseException):
            raise res_val

        return res_val

    async def wait_stop(self) -> Exception:
        return await self._stop_reason

    async def write_with_ack_future(
        self, messages: List[PublicMessage]
    ) -> List[asyncio.Future]:
        # todo check internal buffer limit
        self._check_stop()

        if self._settings.auto_seqno:
            await self.wait_init()

        internal_messages = self._prepare_internal_messages(messages)
        messages_future = [self._loop.create_future() for _ in internal_messages]

        self._messages_future.extend(messages_future)

        if self._codec == PublicCodec.RAW:
            self._add_messages_to_send_queue(internal_messages)
        else:
            self._messages_for_encode.put_nowait(internal_messages)

        return messages_future

    def _add_messages_to_send_queue(self, internal_messages: List[InternalMessage]):
        self._messages.extend(internal_messages)
        for m in internal_messages:
            self._new_messages.put_nowait(m)

    def _prepare_internal_messages(
        self, messages: List[PublicMessage]
    ) -> List[InternalMessage]:
        if self._settings.auto_created_at:
            now = datetime.datetime.now()
        else:
            now = None

        res = []
        for m in messages:
            internal_message = InternalMessage(m)
            if self._settings.auto_seqno:
                if internal_message.seq_no is None:
                    self._last_known_seq_no += 1
                    internal_message.seq_no = self._last_known_seq_no
                else:
                    raise TopicWriterError(
                        "Explicit seqno and auto_seq setting is mutual exclusive"
                    )
            else:
                if internal_message.seq_no is None or internal_message.seq_no == 0:
                    raise TopicWriterError(
                        "Empty seqno and auto_seq setting is disabled"
                    )
                elif internal_message.seq_no <= self._last_known_seq_no:
                    raise TopicWriterError(
                        "Message seqno is duplicated: %s" % internal_message.seq_no
                    )
                else:
                    self._last_known_seq_no = internal_message.seq_no

            if self._settings.auto_created_at:
                if internal_message.created_at is not None:
                    raise TopicWriterError(
                        "Explicit set auto_created_at and setting auto_created_at is mutual exclusive"
                    )
                else:
                    internal_message.created_at = now

            res.append(internal_message)

        return res

    def _check_stop(self):
        if self._stop_reason.done():
            raise self._stop_reason.result()

    async def _connection_loop(self):
        retry_settings = RetrySettings()  # todo

        while True:
            attempt = 0  # todo calc and reset
            pending = []

            # noinspection PyBroadException
            stream_writer = None
            try:
                stream_writer = await WriterAsyncIOStream.create(
                    self._driver, self._init_message, self._get_token
                )
                try:
                    self._last_known_seq_no = stream_writer.last_seqno
                    self._init_info.set_result(
                        PublicWriterInitInfo(
                            last_seqno=stream_writer.last_seqno,
                            supported_codecs=stream_writer.supported_codecs,
                        )
                    )
                except asyncio.InvalidStateError:
                    pass

                self._stream_connected.set()

                send_loop = asyncio.create_task(
                    self._send_loop(stream_writer), name="writer send loop"
                )
                receive_loop = asyncio.create_task(
                    self._read_loop(stream_writer), name="writer receive loop"
                )

                pending = [send_loop, receive_loop]

                done, pending = await asyncio.wait(
                    [send_loop, receive_loop], return_when=asyncio.FIRST_COMPLETED
                )
                stream_writer.close()
                done.pop().result()
            except issues.Error as err:
                # todo log error
                print(err)

                err_info = check_retriable_error(err, retry_settings, attempt)
                if not err_info.is_retriable:
                    self._stop(err)
                    return

                await asyncio.sleep(err_info.sleep_timeout_seconds)

            except (asyncio.CancelledError, Exception) as err:
                self._stop(err)
                return
            finally:
                if stream_writer:
                    stream_writer.close()
                if len(pending) > 0:
                    for task in pending:
                        task.cancel()
                    await asyncio.wait(pending)

    async def _encode_loop(self):
        while True:
            messages = await self._messages_for_encode.get()
            while not self._messages_for_encode.empty():
                messages.extend(self._messages_for_encode.get_nowait())

            batch_codec = await self._codec_selector(messages)
            await self._encode_data_inplace(batch_codec, messages)
            self._add_messages_to_send_queue(messages)

    async def _encode_data_inplace(
        self, codec: PublicCodec, messages: List[InternalMessage]
    ):
        if codec == PublicCodec.RAW:
            return

        eventloop = asyncio.get_running_loop()
        encode_waiters = []
        encoder_function = self._codec_functions[codec]

        for message in messages:
            encoded_data_futures = eventloop.run_in_executor(
                self._encode_executor, encoder_function, message.get_bytes()
            )
            encode_waiters.append(encoded_data_futures)

        encoded_datas = await asyncio.gather(*encode_waiters)

        for index, data in enumerate(encoded_datas):
            message = messages[index]
            message.codec = codec
            message.data = data

    async def _codec_selector(self, messages: List[InternalMessage]) -> PublicCodec:
        if self._codec is not None:
            return self._codec

        if self._codec_selector_last_codec is None:
            available_codecs = await self._get_available_codecs()

            if self._codec_selector_batch_num < len(available_codecs):
                codec_index = self._codec_selector_batch_num % len(available_codecs)
                codec = available_codecs[codec_index]
            else:
                codec = await self._codec_selector_by_check_compress(messages)
                self._codec_selector_last_codec = codec
        else:
            if (
                self._codec_selector_batch_num
                % self._codec_selector_check_batches_interval
                == 0
            ):
                self._codec_selector_last_codec = (
                    await self._codec_selector_by_check_compress(messages)
                )
            codec = self._codec_selector_last_codec
        self._codec_selector_batch_num += 1
        return codec

    async def _get_available_codecs(self) -> List[PublicCodec]:
        info = await self.wait_init()
        topic_supported_codecs = info.supported_codecs
        if not topic_supported_codecs:
            topic_supported_codecs = [PublicCodec.RAW, PublicCodec.GZIP]

        res = []
        for codec in topic_supported_codecs:
            if codec in self._codec_functions:
                res.append(codec)

        if not res:
            raise TopicWriterError("Writer does not support topic's codecs")

        res.sort()

        return res

    async def _codec_selector_by_check_compress(
        self, messages: List[InternalMessage]
    ) -> PublicCodec:
        test_messages = messages
        if len(test_messages) > 10:
            test_messages = test_messages[:10]

        available_codecs = await self._get_available_codecs()
        if len(available_codecs) == 1:
            return available_codecs[0]

        def get_compressed_size(codec) -> int:
            s = 0
            f = self._codec_functions[codec]

            for m in test_messages:
                encoded = f(m.get_bytes())
                s += len(encoded)

            return s

        def select_codec() -> PublicCodec:
            min_codec = available_codecs[0]
            min_size = get_compressed_size(min_codec)
            for codec in available_codecs[1:]:
                size = get_compressed_size(codec)
                if size < min_size:
                    min_codec = codec
                    min_size = size
            return min_codec

        loop = asyncio.get_running_loop()
        codec = await loop.run_in_executor(self._encode_executor, select_codec)
        return codec

    async def _read_loop(self, writer: "WriterAsyncIOStream"):
        while True:
            resp = await writer.receive()

            for ack in resp.acks:
                self._handle_receive_ack(ack)

    def _handle_receive_ack(self, ack):
        current_message = self._messages.popleft()
        message_future = self._messages_future.popleft()
        if current_message.seq_no != ack.seq_no:
            raise TopicWriterError(
                "internal error - receive unexpected ack. Expected seqno: %s, received seqno: %s"
                % (current_message.seq_no, ack.seq_no)
            )
        message_future.set_result(
            None
        )  # todo - return result with offset or skip status

    async def _send_loop(self, writer: "WriterAsyncIOStream"):
        try:
            messages = list(self._messages)

            last_seq_no = 0
            for m in messages:
                writer.write([m])
                last_seq_no = m.seq_no

            while True:
                m = await self._new_messages.get()  # type: InternalMessage
                if m.seq_no > last_seq_no:
                    writer.write([m])
        except Exception as e:
            self._stop(e)
        finally:
            pass

    def _stop(self, reason: Exception):
        if reason is None:
            raise Exception("writer stop reason can not be None")

        if self._stop_reason.done():
            return

        self._stop_reason.set_result(reason)

    def _get_token(self) -> str:
        raise NotImplementedError()

    async def flush(self):
        self._check_stop()
        if not self._messages_future:
            return

        # wait last message
        await asyncio.wait((self._messages_future[-1],))


class WriterAsyncIOStream:
    # todo slots

    last_seqno: int
    supported_codecs: Optional[List[PublicCodec]]

    _stream: IGrpcWrapperAsyncIO
    _token_getter: TokenGetterFuncType
    _requests: asyncio.Queue
    _responses: AsyncIterator

    def __init__(
        self,
        token_getter: TokenGetterFuncType,
    ):
        self._token_getter = token_getter

    def close(self):
        self._stream.close()

    @staticmethod
    async def create(
        driver: SupportedDriverType,
        init_request: StreamWriteMessage.InitRequest,
        token_getter: TokenGetterFuncType,
    ) -> "WriterAsyncIOStream":
        stream = GrpcWrapperAsyncIO(StreamWriteMessage.FromServer.from_proto)

        await stream.start(
            driver, _apis.TopicService.Stub, _apis.TopicService.StreamWrite
        )

        writer = WriterAsyncIOStream(token_getter)
        await writer._start(stream, init_request)
        return writer

    async def receive(self) -> StreamWriteMessage.WriteResponse:
        while True:
            item = await self._stream.receive()

            if isinstance(item, StreamWriteMessage.WriteResponse):
                return item
            if isinstance(item, UpdateTokenResponse):
                continue

            # todo log unknown messages instead of raise exception
            raise Exception("Unknown message while read writer answers: %s" % item)

    async def _start(
        self, stream: IGrpcWrapperAsyncIO, init_message: StreamWriteMessage.InitRequest
    ):
        stream.write(StreamWriteMessage.FromClient(init_message))

        resp = await stream.receive()
        self._ensure_ok(resp)
        if not isinstance(resp, StreamWriteMessage.InitResponse):
            raise TopicWriterError("Unexpected answer for init request: %s" % resp)

        self.last_seqno = resp.last_seq_no
        self.supported_codecs = [PublicCodec(codec) for codec in resp.supported_codecs]

        self._stream = stream

    @staticmethod
    def _ensure_ok(message: WriterMessagesFromServerToClient):
        if not message.status.is_success():
            raise TopicWriterError(
                "status error from server in writer: %s", message.status
            )

    def write(self, messages: List[InternalMessage]):
        for request in messages_to_proto_requests(messages):
            self._stream.write(request)
