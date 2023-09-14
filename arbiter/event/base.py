#   Copyright 2020 getcarrier.io
#
#   Licensed under the Apache License, Version 2.0 (the "License");
#   you may not use this file except in compliance with the License.
#   You may obtain a copy of the License at
#
#       http://www.apache.org/licenses/LICENSE-2.0
#
#   Unless required by applicable law or agreed to in writing, software
#   distributed under the License is distributed on an "AS IS" BASIS,
#   WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#   See the License for the specific language governing permissions and
#   limitations under the License.


import time
import json
import threading
import pika
import ssl
import logging


class BaseEventHandler(threading.Thread):
    """ Basic representation of events handler"""

    def __init__(self, settings, subscriptions, state, wait_time=2.0):
        super().__init__(daemon=True)
        self.settings = settings
        self.state = state
        self.subscriptions = subscriptions
        self._stop_event = threading.Event()
        self.started = False
        self.wait_time = wait_time

    def _get_connection(self):
        ssl_options = None
        #
        if self.settings.use_ssl:
            ssl_context = ssl.create_default_context()
            if self.settings.ssl_verify:
                ssl_context.verify_mode = ssl.CERT_REQUIRED
                ssl_context.check_hostname = True
                ssl_context.load_default_certs()
            else:
                ssl_context.check_hostname = False
                ssl_context.verify_mode = ssl.CERT_NONE
            ssl_server_hostname = self.settings.host
            #
            ssl_options = pika.SSLOptions(ssl_context, ssl_server_hostname)
        #
        connection = pika.BlockingConnection(
            pika.ConnectionParameters(
                host=self.settings.host,
                port=self.settings.port,
                virtual_host=self.settings.vhost,
                credentials=pika.PlainCredentials(
                    self.settings.user,
                    self.settings.password
                ),
                ssl_options=ssl_options,
            )
        )
        return connection

    def _get_channel(self, connection=None):
        if not connection:
            connection = self._get_connection()
        channel = connection.channel()
        if self.settings.queue:
            channel.queue_declare(
                queue=self.settings.queue, durable=True
            )
        channel.exchange_declare(
            exchange=self.settings.all,
            exchange_type="fanout", durable=True
        )
        channel = self._connect_to_specific_queue(channel)
        return channel

    def _connect_to_specific_queue(self, channel):
        raise NotImplemented

    def wait_running(self, timeout=0):
        attempts = 0
        attempts_limit = timeout * 2
        while not self.started:
            time.sleep(0.5)
            attempts += 1
            if attempts == attempts_limit:
                break

    def run(self):
        """ Run handler thread """
        logging.info("Starting handler thread")
        channel = None
        while not self.stopped():
            logging.info("Starting handler consuming")
            try:
                channel = self._get_channel()
                logging.info("[%s] Waiting for task events", self.ident)
                self.started = True
                channel.start_consuming()
            except pika.exceptions.ConnectionClosedByBroker:
                logging.info("Connection Closed by Broker")
                time.sleep(5.0)
                continue
            except pika.exceptions.AMQPChannelError:
                logging.info("AMQPChannelError")
            except pika.exceptions.StreamLostError:
                logging.info("Recovering from error")
                time.sleep(5.0)
                continue
            except pika.exceptions.AMQPConnectionError:
                logging.info("Recovering from error")
                time.sleep(5.0)
                continue
        channel.stop_consuming()

    def stop(self):
        self._stop_event.set()

    def stopped(self):
        return self._stop_event.is_set()

    @staticmethod
    def respond(channel, message, queue, delay=0):
        logging.debug(message)
        if delay and isinstance(delay, int):
            time.sleep(delay)
        channel.basic_publish(
            exchange="", routing_key=queue,
            body=json.dumps(message).encode("utf-8"),
            properties=pika.BasicProperties(
                delivery_mode=2,
            )
        )

    def queue_event_callback(self, channel, method, properties, body):  # pylint: disable=R0912,R0915
        raise NotImplemented
