import json
import logging
import os
import ssl
from typing import Any, Dict, Optional, Text, Union

import typing

if typing.TYPE_CHECKING:
    import pika

from rasa.utils.common import class_from_module_path
from rasa.utils.endpoints import EndpointConfig

logger = logging.getLogger(__name__)


def from_endpoint_config(
    broker_config: Optional[EndpointConfig]
) -> Optional["EventChannel"]:
    """Instantiate an event channel based on its configuration."""

    if broker_config is None:
        return None
    elif broker_config.type == "pika" or broker_config.type is None:
        return PikaProducer.from_endpoint_config(broker_config)
    elif broker_config.type.lower() == "sql":
        return SQLProducer.from_endpoint_config(broker_config)
    elif broker_config.type == "file":
        return FileProducer.from_endpoint_config(broker_config)
    elif broker_config.type == "kafka":
        return KafkaProducer.from_endpoint_config(broker_config)
    else:
        return load_event_channel_from_module_string(broker_config)


def load_event_channel_from_module_string(
    broker_config: EndpointConfig
) -> Optional["EventChannel"]:
    """Instantiate an event channel based on its class name."""

    try:
        event_channel = class_from_module_path(broker_config.type)
        return event_channel.from_endpoint_config(broker_config)
    except (AttributeError, ImportError) as e:
        logger.warning(
            "EventChannel type '{}' not found. "
            "Not using any event channel. Error: {}".format(broker_config.type, e)
        )
        return None


class EventChannel(object):
    @classmethod
    def from_endpoint_config(cls, broker_config: EndpointConfig) -> "EventChannel":
        raise NotImplementedError(
            "Event broker must implement the `from_endpoint_config` method."
        )

    def publish(self, event: Dict[Text, Any]) -> None:
        """Publishes a json-formatted Rasa Core event into an event queue."""

        raise NotImplementedError("Event broker must implement the `publish` method.")


def create_rabbitmq_ssl_options(
    rabbitmq_host: Optional[Text] = None
) -> Optional["pika.SSLOptions"]:
    """Create RabbitMQ SSL options.

    Requires the following environment variables to be set:

    RABBITMQ_SSL_CLIENT_CERTIFICATE - path to the SSL client certificate (required)
    RABBITMQ_SSL_CLIENT_KEY - path to the SSL client key (required)
    RABBITMQ_SSL_CA_FILE - path to the SSL CA file for verification (optional)

    Details on how to enable RabbitMQ TLS support can be found here:
    https://www.rabbitmq.com/ssl.html#enabling-tls

    Args:
        rabbitmq_host: RabbitMQ hostname

    Returns:
        Pika SSL context of type `pika.SSLOptions` if
        the RABBITMQ_SSL_CLIENT_CERTIFICATE and RABBITMQ_SSL_CLIENT_KEY
        environment variables are valid paths, else `None`.

    """

    import pika

    client_certificate_path = os.environ.get("RABBITMQ_SSL_CLIENT_CERTIFICATE")
    client_key_path = os.environ.get("RABBITMQ_SSL_CLIENT_KEY")

    if client_certificate_path and client_key_path:
        logger.debug(
            "Configuring SSL context for RabbitMQ host '{}'.".format(rabbitmq_host)
        )

        ca_file_path = os.environ.get("RABBITMQ_SSL_CA_FILE")

        if ca_file_path:
            logger.debug("Using certificate verification with provided CA file.")
            context = ssl.create_default_context(cafile=ca_file_path)
        else:
            context = ssl.create_default_context(purpose=ssl.Purpose.CLIENT_AUTH)

        context.load_cert_chain(client_certificate_path, client_key_path)

        return pika.SSLOptions(context, rabbitmq_host)
    else:
        return None


class PikaProducer(EventChannel):
    def __init__(
        self,
        host: Text,
        username: Optional[Text],
        password: Optional[Text],
        port: Union[int, Text] = 5672,
        queue: Text = "rasa_core_events",
        loglevel: Text = logging.WARNING,
    ):
        import pika

        logging.getLogger("pika").setLevel(loglevel)

        self.queue = queue
        self.host = host
        self.port = port
        self.connection = None
        self.channel = None
        if username and password:
            self.credentials = pika.PlainCredentials(username, password)
        else:
            self.credentials = None

    @classmethod
    def from_endpoint_config(
        cls, broker_config: Optional["EndpointConfig"]
    ) -> Optional["PikaProducer"]:
        if broker_config is None:
            return None

        return cls(broker_config.url, **broker_config.kwargs)

    def publish(self, event):
        self._open_connection()
        self._publish(json.dumps(event))
        self._close()

    def _open_connection(self):
        import pika

        if self.host.startswith("amqp"):
            # user supplied a amqp url containing all the info
            parameters = pika.URLParameters(self.host)
            parameters.connection_attempts = 20
            parameters.retry_delay = 5
            if self.credentials:
                parameters = self.credentials
        else:
            # host seems to be just the host, so we use our parameters
            parameters = pika.ConnectionParameters(
                self.host,
                port=self.port,
                credentials=self.credentials,
                connection_attempts=20,
                retry_delay=5,
                ssl_options=create_rabbitmq_ssl_options(self.host),
            )
        self.connection = pika.BlockingConnection(parameters)
        self.channel = self.connection.channel()
        self.channel.queue_declare(self.queue, durable=True)

    def _publish(self, body):
        self.channel.basic_publish("", self.queue, body)
        logger.debug(
            "Published pika events to queue {} at "
            "{}:\n{}".format(self.queue, self.host, body)
        )

    def _close(self):
        self.connection.close()


class FileProducer(EventChannel):
    """Log events to a file in json format.

    There will be one event per line and each event is stored as json."""

    DEFAULT_LOG_FILE_NAME = "rasa_event.log"

    def __init__(self, path: Optional[Text] = None) -> None:
        self.path = path or self.DEFAULT_LOG_FILE_NAME
        self.event_logger = self._event_logger()

    @classmethod
    def from_endpoint_config(
        cls, broker_config: Optional["EndpointConfig"]
    ) -> Optional["FileProducer"]:
        if broker_config is None:
            return None

        # noinspection PyArgumentList
        return cls(**broker_config.kwargs)

    def _event_logger(self):
        """Instantiate the file logger."""

        logger_file = self.path
        # noinspection PyTypeChecker
        query_logger = logging.getLogger("event-logger")
        query_logger.setLevel(logging.INFO)
        handler = logging.FileHandler(logger_file)
        handler.setFormatter(logging.Formatter("%(message)s"))
        query_logger.propagate = False
        query_logger.addHandler(handler)

        logger.info("Logging events to '{}'.".format(logger_file))

        return query_logger

    def publish(self, event: Dict) -> None:
        """Write event to file."""

        self.event_logger.info(json.dumps(event))
        self.event_logger.handlers[0].flush()


class KafkaProducer(EventChannel):
    def __init__(
        self,
        host,
        sasl_username=None,
        sasl_password=None,
        ssl_cafile=None,
        ssl_certfile=None,
        ssl_keyfile=None,
        ssl_check_hostname=False,
        topic="rasa_core_events",
        security_protocol="SASL_PLAINTEXT",
        loglevel=logging.ERROR,
    ):

        self.producer = None
        self.host = host
        self.topic = topic
        self.security_protocol = security_protocol
        self.sasl_username = sasl_username
        self.sasl_password = sasl_password
        self.ssl_cafile = ssl_cafile
        self.ssl_certfile = ssl_certfile
        self.ssl_keyfile = ssl_keyfile
        self.ssl_check_hostname = ssl_check_hostname

        logging.getLogger("kafka").setLevel(loglevel)

    @classmethod
    def from_endpoint_config(cls, broker_config) -> Optional["KafkaProducer"]:
        if broker_config is None:
            return None

        return cls(broker_config.url, **broker_config.kwargs)

    def publish(self, event):
        self._create_producer()
        self._publish(event)
        self._close()

    def _create_producer(self):
        import kafka

        if self.security_protocol == "SASL_PLAINTEXT":
            self.producer = kafka.KafkaProducer(
                bootstrap_servers=[self.host],
                value_serializer=lambda v: json.dumps(v).encode("utf-8"),
                sasl_plain_username=self.sasl_username,
                sasl_plain_password=self.sasl_password,
                sasl_mechanism="PLAIN",
                security_protocol=self.security_protocol,
            )
        elif self.security_protocol == "SSL":
            self.producer = kafka.KafkaProducer(
                bootstrap_servers=[self.host],
                value_serializer=lambda v: json.dumps(v).encode("utf-8"),
                ssl_cafile=self.ssl_cafile,
                ssl_certfile=self.ssl_certfile,
                ssl_keyfile=self.ssl_keyfile,
                ssl_check_hostname=False,
                security_protocol=self.security_protocol,
            )

    def _publish(self, event):
        self.producer.send(self.topic, event)

    def _close(self):
        self.producer.close()


class SQLProducer(EventChannel):
    """Save events into an SQL database.

    All events will be stored in a table called `events`.

    """

    from sqlalchemy.ext.declarative import declarative_base

    Base = declarative_base()

    class SQLBrokerEvent(Base):
        from sqlalchemy import Column, Integer, String, Text

        __tablename__ = "events"
        id = Column(Integer, primary_key=True)
        sender_id = Column(String(255))
        data = Column(Text)

    def __init__(
        self,
        dialect: Text = "sqlite",
        host: Optional[Text] = None,
        port: Optional[int] = None,
        db: Text = "events.db",
        username: Optional[Text] = None,
        password: Optional[Text] = None,
    ):
        from rasa.core.tracker_store import SQLTrackerStore
        import sqlalchemy
        import sqlalchemy.orm

        engine_url = SQLTrackerStore.get_db_url(
            dialect, host, port, db, username, password
        )

        logger.debug("SQLProducer: Connecting to database: '{}'.".format(engine_url))

        self.engine = sqlalchemy.create_engine(engine_url)
        self.Base.metadata.create_all(self.engine)
        self.session = sqlalchemy.orm.sessionmaker(bind=self.engine)()

    @classmethod
    def from_endpoint_config(cls, broker_config: EndpointConfig) -> "EventChannel":
        return cls(host=broker_config.url, **broker_config.kwargs)

    def publish(self, event: Dict[Text, Any]) -> None:
        """Publishes a json-formatted Rasa Core event into an event queue."""
        self.session.add(
            self.SQLBrokerEvent(
                sender_id=event.get("sender_id"), data=json.dumps(event)
            )
        )
        self.session.commit()
