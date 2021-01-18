"""
Persistence layers supporting idempotency
"""

import datetime
import hashlib
import json
import logging
from abc import ABC, abstractmethod
from typing import Any, Dict, Optional, Union

import boto3
import jmespath
from botocore.config import Config

from .cache_dict import LRUDict
from .exceptions import InvalidStatusError, ItemAlreadyExistsError, ItemNotFoundError

logger = logging.getLogger(__name__)

STATUS_CONSTANTS = {
    "NOTEXISTING": "DOESNOTEXIST",
    "INPROGRESS": "INPROGRESS",
    "COMPLETED": "COMPLETED",
    "EXPIRED": "EXPIRED",
    "ERROR": "ERROR",
}


class DataRecord:
    """
    Data Class for idempotency records.
    """

    def __init__(
        self, idempotency_key, status: str = None, expiry_timestamp: int = None, response_data: str = None
    ) -> None:
        """

        Parameters
        ----------
        idempotency_key: str
            hashed representation of the idempotent data
        status: str, optional
            status of the idempotent record
        expiry_timestamp: int, optional
            time before the record should expire, in milliseconds
        response_data: str, optional
            response data from previous executions using the record
        """
        self.idempotency_key = idempotency_key
        self.expiry_timestamp = expiry_timestamp
        self._status = status
        self.response_data = response_data

    @property
    def is_expired(self) -> bool:
        """
        Check if data record is expired

        Returns
        -------
        bool
            Whether the record is currently expired or not
        """
        if self.expiry_timestamp:
            if int(datetime.datetime.now().timestamp()) > self.expiry_timestamp:
                return True
        return False

    @property
    def status(self) -> str:
        """
        Get status of data record

        Returns
        -------
        str
        """
        if self.is_expired:
            return STATUS_CONSTANTS["EXPIRED"]

        if self._status in STATUS_CONSTANTS.values():
            return self._status
        else:
            raise InvalidStatusError(self._status)

    def response_json_as_dict(self) -> dict:
        """
        Get response data deserialized to python dict

        Returns
        -------
        dict
            previous response data deserialized
        """
        return json.loads(self.response_data)


class BasePersistenceLayer(ABC):
    """
    Abstract Base Class for Idempotency persistence layer.
    """

    def __init__(
        self, event_key: str, expires_after: int = 3600, use_local_cache: bool = False, local_cache_maxsize: int = 1024,
    ) -> None:
        """
        Initialize the base persistence layer

        Parameters
        ----------
        event_key: str
            A jmespath expression to extract the idempotency key from the event record
        expires_after: int
            The number of milliseconds to wait before a record is expired
        use_local_cache: bool, optional
            Whether to locally cache idempotency results, by default False
        local_cache_maxsize: int, optional
            Max number of items to store in local cache, by default 1024
        """
        self.event_key = event_key
        self.event_key_jmespath = jmespath.compile(event_key)
        self.expires_after = expires_after
        self.use_local_cache = use_local_cache
        if self.use_local_cache:
            self._cache = LRUDict(max_size=local_cache_maxsize)

    def get_hashed_idempotency_key(self, lambda_event: Dict[str, Any]) -> str:
        """
        Extract data from lambda event using event key jmespath, and return a hashed representation

        Parameters
        ----------
        lambda_event: Dict[str, Any]
            Lambda event

        Returns
        -------
        str
            md5 hash of the data extracted by the jmespath expression

        """
        data = self.event_key_jmespath.search(lambda_event)

        # The following hash is not used in any security context. It is only used
        # to generate unique values.
        hashed_data = hashlib.md5(json.dumps(data).encode())  # nosec
        return hashed_data.hexdigest()

    def _get_expiry_timestamp(self) -> int:
        """

        Returns
        -------
        int
            unix timestamp of expiry date for idempotency record

        """
        now = datetime.datetime.now()
        period = datetime.timedelta(seconds=self.expires_after)
        return int((now + period).timestamp())

    def _save_to_cache(self, data_record: DataRecord):
        self._cache[data_record.idempotency_key] = data_record

    def _retrieve_from_cache(self, idempotency_key: str):
        cached_record = self._cache.get(idempotency_key)
        if cached_record:
            if cached_record.is_expired:
                logger.debug(f"Removing expired local cache record for idempotency key: {idempotency_key}")
                self._delete_from_cache(idempotency_key)
            else:
                return cached_record

    def _delete_from_cache(self, idempotency_key: str):
        del self._cache[idempotency_key]

    def save_success(self, event: Dict[str, Any], result: dict) -> None:
        """
        Save record of function's execution completing succesfully

        Parameters
        ----------
        event: Dict[str, Any]
            Lambda event
        result: dict
            The response from lambda handler
        """
        response_data = json.dumps(result)

        data_record = DataRecord(
            idempotency_key=self.get_hashed_idempotency_key(event),
            status=STATUS_CONSTANTS["COMPLETED"],
            expiry_timestamp=self._get_expiry_timestamp(),
            response_data=response_data,
        )
        logger.debug(
            f"Lambda successfully executed. Saving record to persistence store with "
            f"idempotency key: {data_record.idempotency_key}"
        )
        self._update_record(data_record=data_record)

        if self.use_local_cache:
            self._save_to_cache(data_record)

    def save_inprogress(self, event: Dict[str, Any]) -> None:
        """
        Save record of function's execution being in progress

        Parameters
        ----------
        event: Dict[str, Any]
            Lambda event
        """
        data_record = DataRecord(
            idempotency_key=self.get_hashed_idempotency_key(event),
            status=STATUS_CONSTANTS["INPROGRESS"],
            expiry_timestamp=self._get_expiry_timestamp(),
        )

        logger.debug(f"Saving in progress record for idempotency key: {data_record.idempotency_key}")
        self._put_record(data_record)

        if self.use_local_cache:
            self._save_to_cache(data_record)

    def save_error(self, event: Dict[str, Any], exception: Exception):
        """
        Save record of lambda handler raising an exception

        Parameters
        ----------
        event: Dict[str, Any]
            Lambda event
        exception
            The exception raised by the lambda handler
        """
        data_record = DataRecord(
            idempotency_key=self.get_hashed_idempotency_key(event),
            status=STATUS_CONSTANTS["ERROR"],
            expiry_timestamp=self._get_expiry_timestamp(),
        )

        logger.debug(
            f"Lambda raised an exception ({type(exception).__name__}). Clearing in progress record in persistence "
            f"store for idempotency key: {data_record.idempotency_key}"
        )
        self._delete_record(data_record)

        if self.use_local_cache:
            self._delete_from_cache(data_record.idempotency_key)

    def get_record(self, lambda_event) -> DataRecord:
        """
        Calculate idempotency key for lambda_event, then retrieve item from persistence store using idempotency key
        and return it as a DataRecord instance.and return it as a DataRecord instance.

        Parameters
        ----------
        lambda_event: Dict[str, Any]

        Returns
        -------
        DataRecord
            DataRecord representation of existing record found in persistence store

        Raises
        ------
        ItemNotFound
            Exception raised if no record exists in persistence store with the idempotency key
        """

        idempotency_key = self.get_hashed_idempotency_key(lambda_event)

        if self.use_local_cache:
            cached_record = self._retrieve_from_cache(idempotency_key)
            if cached_record:
                logger.debug(f"Idempotency record found in cache with idempotency key: {idempotency_key}")
                return cached_record

        return self._get_record(idempotency_key)

    @abstractmethod
    def _get_record(self, idempotency_key) -> DataRecord:
        """
        Retrieve item from persistence store using idempotency key and return it as a DataRecord instance.

        Parameters
        ----------
        idempotency_key

        Returns
        -------
        DataRecord
            DataRecord representation of existing record found in persistence store

        Raises
        ------
        ItemNotFound
            Exception raised if no record exists in persistence store with the idempotency key
        """
        raise NotImplementedError

    @abstractmethod
    def _put_record(self, data_record: DataRecord) -> None:
        """
        Add a DataRecord to persistence store if it does not already exist with that key. Raise ItemAlreadyExists
        if an entry already exists.

        Parameters
        ----------
        data_record: DataRecord
            DataRecord instance
        """

        raise NotImplementedError

    @abstractmethod
    def _update_record(self, data_record: DataRecord) -> None:
        """
        Update item in persistence store

        Parameters
        ----------
        data_record: DataRecord
            DataRecord instance
        """

        raise NotImplementedError

    @abstractmethod
    def _delete_record(self, data_record: DataRecord) -> None:
        """
        Remove item from persistence store
        Parameters
        ----------
        data_record: DataRecord
            DataRecord instance
        """

        raise NotImplementedError


class DynamoDBPersistenceLayer(BasePersistenceLayer):
    def __init__(
        self,
        table_name: str,  # Can we use the lambda function name?
        key_attr: Optional[str] = "id",
        expiry_attr: Optional[str] = "expiration",
        status_attr: Optional[str] = "status",
        data_attr: Optional[str] = "data",
        boto_config: Optional[Config] = None,
        *args,
        **kwargs,
    ):
        """
        Initialize the DynamoDB client

        Parameters
        ----------
        table_name: str
            Name of the table to use for storing execution records
        key_attr: str, optional
            DynamoDB attribute name for key, by default "id"
        expiry_attr: str, optional
            DynamoDB attribute name for expiry timestamp, by default "expiration"
        status_attr: str, optional
            DynamoDB attribute name for status, by default "status"
        data_attr: str, optional
            DynamoDB attribute name for response data, by default "data"
        boto_config: botocore.config.Config, optional
            Botocore configuration to pass during client initialization
        args
        kwargs

        Examples
        --------
        **Create a DynamoDB persistence layer with custom settings**
            >>> from aws_lambda_powertools.utilities.idempotency import idempotent, DynamoDBPersistenceLayer
            >>>
            >>> persistence_store = DynamoDBPersistenceLayer(event_key="body", table_name="idempotency_store")
            >>>
            >>> @idempotent(persistence=persistence_store)
            >>> def handler(event, context):
            >>>     return {"StatusCode": 200}
        """

        boto_config = boto_config or Config()
        self._ddb_resource = boto3.resource("dynamodb", config=boto_config)
        self.table_name = table_name
        self.table = self._ddb_resource.Table(self.table_name)
        self.key_attr = key_attr
        self.expiry_attr = expiry_attr
        self.status_attr = status_attr
        self.data_attr = data_attr
        super(DynamoDBPersistenceLayer, self).__init__(*args, **kwargs)

    def _item_to_data_record(self, item: Dict[str, Union[str, int]]) -> DataRecord:
        """
        Translate raw item records from DynamoDB to DataRecord

        Parameters
        ----------
        item: Dict[str, Union[str, int]]
            Item format from dynamodb response

        Returns
        -------
        DataRecord
            representation of item

        """
        return DataRecord(
            idempotency_key=item[self.key_attr],
            status=item[self.status_attr],
            expiry_timestamp=item[self.expiry_attr],
            response_data=item.get(self.data_attr),
        )

    def _get_record(self, idempotency_key) -> DataRecord:
        response = self.table.get_item(Key={self.key_attr: idempotency_key}, ConsistentRead=True)

        try:
            item = response["Item"]
        except KeyError:
            raise ItemNotFoundError
        return self._item_to_data_record(item)

    def _put_record(self, data_record: DataRecord) -> None:
        now = datetime.datetime.now()
        try:
            logger.debug(f"Putting record for idempotency key: {data_record.idempotency_key}")
            self.table.put_item(
                Item={
                    self.key_attr: data_record.idempotency_key,
                    "expiration": data_record.expiry_timestamp,
                    "status": STATUS_CONSTANTS["INPROGRESS"],
                },
                ConditionExpression=f"attribute_not_exists({self.key_attr}) OR expiration < :now",
                ExpressionAttributeValues={":now": int(now.timestamp())},
            )
        except self._ddb_resource.meta.client.exceptions.ConditionalCheckFailedException:
            logger.debug(f"Failed to put record for already existing idempotency key: {data_record.idempotency_key}")
            raise ItemAlreadyExistsError

    def _update_record(self, data_record: DataRecord):
        logger.debug(f"Updating record for idempotency key: {data_record.idempotency_key}")
        self.table.update_item(
            Key={self.key_attr: data_record.idempotency_key},
            UpdateExpression="SET #response_data = :response_data, #expiry = :expiry, #status = :status",
            ExpressionAttributeValues={
                ":expiry": data_record.expiry_timestamp,
                ":response_data": data_record.response_data,
                ":status": data_record.status,
            },
            ExpressionAttributeNames={
                "#response_data": self.data_attr,
                "#expiry": self.expiry_attr,
                "#status": self.status_attr,
            },
        )

    def _delete_record(self, data_record: DataRecord) -> None:
        logger.debug(f"Deleting record for idempotency key: {data_record.idempotency_key}")
        self.table.delete_item(Key={self.key_attr: data_record.idempotency_key},)