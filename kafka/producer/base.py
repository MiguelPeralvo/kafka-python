from __future__ import absolute_import

import atexit
import logging
import time

try:
    from queue import Empty, Full, Queue
except ImportError:
    from Queue import Empty, Full, Queue
from collections import defaultdict

from threading import Thread, Event

import six

from kafka.common import (
    ProduceRequest, ProduceResponse, TopicAndPartition, RetryOptions,
    kafka_errors, UnsupportedCodecError, FailedPayloadsError,
    RequestTimedOutError, AsyncProducerQueueFull, UnknownError
)
from kafka.common import (
    RETRY_ERROR_TYPES, RETRY_BACKOFF_ERROR_TYPES, RETRY_REFRESH_ERROR_TYPES)

from kafka.protocol import CODEC_NONE, ALL_CODECS, create_message_set
from kafka.util import kafka_bytestring

log = logging.getLogger("kafka")

BATCH_SEND_DEFAULT_INTERVAL = 20
BATCH_SEND_MSG_COUNT = 20

# unlimited
ASYNC_QUEUE_MAXSIZE = 0
ASYNC_QUEUE_PUT_TIMEOUT = 0
# no retries by default
ASYNC_RETRY_LIMIT = 0
ASYNC_RETRY_BACKOFF_MS = 0
ASYNC_RETRY_ON_TIMEOUTS = False

STOP_ASYNC_PRODUCER = -1


def _send_upstream(queue, client, codec, batch_time, batch_size,
                   req_acks, ack_timeout, retry_options, stop_event):
    """
    Listen on the queue for a specified number of messages or till
    a specified timeout and send them upstream to the brokers in one
    request
    """
    reqs = {}
    client.reinit()

    while not stop_event.is_set():
        timeout = batch_time

        # it's a simplification: we're comparing message sets and
        # messages: each set can contain [1..batch_size] messages
        count = batch_size - len(reqs)
        send_at = time.time() + timeout
        msgset = defaultdict(list)

        # Keep fetching till we gather enough messages or a
        # timeout is reached
        while count > 0 and timeout >= 0:
            try:
                topic_partition, msg, key = queue.get(timeout=timeout)
            except Empty:
                break

            # Check if the controller has requested us to stop
            if topic_partition == STOP_ASYNC_PRODUCER:
                stop_event.set()
                break

            # Adjust the timeout to match the remaining period
            count -= 1
            timeout = send_at - time.time()
            msgset[topic_partition].append((msg, key))

        # Send collected requests upstream
        for topic_partition, msg in msgset.items():
            messages = create_message_set(msg, codec, key)
            req = ProduceRequest(topic_partition.topic,
                                 topic_partition.partition,
                                 tuple(messages))
            reqs[req] = 0

        if not reqs:
            continue

        reqs_to_retry, error_cls = [], None
        do_backoff, do_refresh = False, False

        def _handle_error(error_cls, reqs, all_retries):
            if ((error_cls == RequestTimedOutError and
                 retry_options.retry_on_timeouts) or
                    error_cls in RETRY_ERROR_TYPES):
                all_retries += reqs
            if error_cls in RETRY_BACKOFF_ERROR_TYPES:
                do_backoff = True
            if error_cls in RETRY_REFRESH_ERROR_TYPES:
                do_refresh = True

        try:
            reply = client.send_produce_request(reqs.keys(),
                                                acks=req_acks,
                                                timeout=ack_timeout,
                                                fail_on_error=False)
            for i, response in enumerate(reply):
                if isinstance(response, FailedPayloadsError):
                    _handle_error(FailedPayloadsError, response.failed_payloads, reqs_to_retry)
                elif isinstance(response, ProduceResponse) and response.error:
                    error_cls = kafka_errors.get(response.error, UnknownError)
                    _handle_error(error_cls, [reqs.keys()[i]], reqs_to_retry)

        except Exception as ex:
            error_cls = kafka_errors.get(type(ex), UnknownError)
            _handle_error(error_cls, reqs.keys(), reqs_to_retry)

        if not reqs_to_retry:
            reqs = {}
            continue

        # doing backoff before next retry
        if do_backoff and retry_options.backoff_ms:
            log.info("Doing backoff for %s(ms)." % retry_options.backoff_ms)
            time.sleep(float(retry_options.backoff_ms) / 1000)

        # refresh topic metadata before next retry
        if do_refresh:
            client.load_metadata_for_topics()

        reqs = dict((key, count + 1) for (key, count) in reqs.items()
                if key in reqs_to_retry and count < retry_options.limit)


class Producer(object):
    """
    Base class to be used by producers

    Arguments:
        client: The Kafka client instance to use
        async: If set to true, the messages are sent asynchronously via another
            thread (process). We will not wait for a response to these
            WARNING!!! current implementation of async producer does not
            guarantee message delivery.  Use at your own risk! Or help us
            improve with a PR!
        req_acks: A value indicating the acknowledgements that the server must
            receive before responding to the request
        ack_timeout: Value (in milliseconds) indicating a timeout for waiting
            for an acknowledgement
        batch_send: If True, messages are send in batches
        batch_send_every_n: If set, messages are send in batches of this size
        batch_send_every_t: If set, messages are send after this timeout
    """

    ACK_NOT_REQUIRED = 0            # No ack is required
    ACK_AFTER_LOCAL_WRITE = 1       # Send response after it is written to log
    ACK_AFTER_CLUSTER_COMMIT = -1   # Send response after data is committed

    DEFAULT_ACK_TIMEOUT = 1000

    def __init__(self, client, async=False,
                 req_acks=ACK_AFTER_LOCAL_WRITE,
                 ack_timeout=DEFAULT_ACK_TIMEOUT,
                 codec=None,
                 batch_send=False,
                 batch_send_every_n=BATCH_SEND_MSG_COUNT,
                 batch_send_every_t=BATCH_SEND_DEFAULT_INTERVAL,
                 async_retry_limit=ASYNC_RETRY_LIMIT,
                 async_retry_backoff_ms=ASYNC_RETRY_BACKOFF_MS,
                 async_retry_on_timeouts=ASYNC_RETRY_ON_TIMEOUTS,
                 async_queue_maxsize=ASYNC_QUEUE_MAXSIZE,
                 async_queue_put_timeout=ASYNC_QUEUE_PUT_TIMEOUT):

        if batch_send:
            async = True
            assert batch_send_every_n > 0
            assert batch_send_every_t > 0
            assert async_queue_maxsize >= 0
        else:
            batch_send_every_n = 1
            batch_send_every_t = 3600

        self.client = client
        self.async = async
        self.req_acks = req_acks
        self.ack_timeout = ack_timeout
        self.stopped = False

        if codec is None:
            codec = CODEC_NONE
        elif codec not in ALL_CODECS:
            raise UnsupportedCodecError("Codec 0x%02x unsupported" % codec)

        self.codec = codec

        if self.async:
            # Messages are sent through this queue
            self.queue = Queue(async_queue_maxsize)
            self.async_queue_put_timeout = async_queue_put_timeout
            async_retry_options = RetryOptions(
                limit=async_retry_limit,
                backoff_ms=async_retry_backoff_ms,
                retry_on_timeouts=async_retry_on_timeouts)
            self.thread_stop_event = Event()
            self.thread = Thread(target=_send_upstream,
                                 args=(self.queue,
                                       self.client.copy(),
                                       self.codec,
                                       batch_send_every_t,
                                       batch_send_every_n,
                                       self.req_acks,
                                       self.ack_timeout,
                                       async_retry_options,
                                       self.thread_stop_event))

            # Thread will die if main thread exits
            self.thread.daemon = True
            self.thread.start()

            def cleanup(obj):
                if obj.stopped:
                    obj.stop()
            self._cleanup_func = cleanup
            atexit.register(cleanup, self)

    def send_messages(self, topic, partition, *msg):
        """
        Helper method to send produce requests
        @param: topic, name of topic for produce request -- type str
        @param: partition, partition number for produce request -- type int
        @param: *msg, one or more message payloads -- type bytes
        @returns: ResponseRequest returned by server
        raises on error

        Note that msg type *must* be encoded to bytes by user.
        Passing unicode message will not work, for example
        you should encode before calling send_messages via
        something like `unicode_message.encode('utf-8')`

        All messages produced via this method will set the message 'key' to Null
        """
        topic = kafka_bytestring(topic)
        return self._send_messages(topic, partition, *msg)

    def _send_messages(self, topic, partition, *msg, **kwargs):
        key = kwargs.pop('key', None)

        # Guarantee that msg is actually a list or tuple (should always be true)
        if not isinstance(msg, (list, tuple)):
            raise TypeError("msg is not a list or tuple!")

        # Raise TypeError if any message is not encoded as bytes
        if any(not isinstance(m, six.binary_type) for m in msg):
            raise TypeError("all produce message payloads must be type bytes")

        # Raise TypeError if topic is not encoded as bytes
        if not isinstance(topic, six.binary_type):
            raise TypeError("the topic must be type bytes")

        # Raise TypeError if the key is not encoded as bytes
        if key is not None and not isinstance(key, six.binary_type):
            raise TypeError("the key must be type bytes")

        if self.async:
            for idx, m in enumerate(msg):
                try:
                    item = (TopicAndPartition(topic, partition), m, key)
                    if self.async_queue_put_timeout == 0:
                        self.queue.put_nowait(item)
                    else:
                        self.queue.put(item, True, self.async_queue_put_timeout)
                except Full:
                    raise AsyncProducerQueueFull(
                        msg[idx:],
                        'Producer async queue overfilled. '
                        'Current queue size %d.' % self.queue.qsize())
            resp = []
        else:
            messages = create_message_set([(m, key) for m in msg], self.codec, key)
            req = ProduceRequest(topic, partition, messages)
            try:
                resp = self.client.send_produce_request([req], acks=self.req_acks,
                                                        timeout=self.ack_timeout)
            except Exception:
                log.exception("Unable to send messages")
                raise
        return resp

    def stop(self, timeout=1):
        """
        Stop the producer. Optionally wait for the specified timeout before
        forcefully cleaning up.
        """
        if self.async:
            self.queue.put((STOP_ASYNC_PRODUCER, None, None))
            self.thread.join(timeout)

            if self.thread.is_alive():
                self.thread_stop_event.set()

        if hasattr(self, '_cleanup_func'):
            # Remove cleanup handler now that we've stopped

            # py3 supports unregistering
            if hasattr(atexit, 'unregister'):
                atexit.unregister(self._cleanup_func) # pylint: disable=no-member

            # py2 requires removing from private attribute...
            else:

                # ValueError on list.remove() if the exithandler no longer exists
                # but that is fine here
                try:
                    atexit._exithandlers.remove((self._cleanup_func, (self,), {}))
                except ValueError:
                    pass

            del self._cleanup_func

        self.stopped = True

    def __del__(self):
        if not self.stopped:
            self.stop()
