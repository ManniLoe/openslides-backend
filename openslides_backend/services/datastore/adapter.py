from collections import defaultdict
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple, Union

import simplejson as json
from simplejson.errors import JSONDecodeError

from ...shared.exceptions import DatastoreException, DatastoreLockedException
from ...shared.filters import And, Filter, FilterOperator, filter_visitor
from ...shared.interfaces.collection_field_lock import (
    CollectionFieldLock,
    CollectionFieldLockWithFilter,
)
from ...shared.interfaces.logging import LoggingModule
from ...shared.interfaces.write_request import WriteRequest
from ...shared.patterns import (
    Collection,
    CollectionField,
    FullQualifiedField,
    FullQualifiedId,
)
from ...shared.typing import DeletedModel, ModelMap
from . import commands
from .deleted_models_behaviour import (
    DeletedModelsBehaviour,
    InstanceAdditionalBehaviour,
)
from .http_engine import HTTPEngine as Engine
from .interface import DatastoreService, PartialModel

# TODO: Use proper typing here.
DatastoreResponse = Any


class DatastoreAdapter(DatastoreService):
    """
    Adapter to connect to readable and writeable datastore.
    """

    # The key of this dictionary is a stringified FullQualifiedId or FullQualifiedField or CollectionField
    locked_fields: Dict[str, CollectionFieldLock]

    def __init__(self, engine: Engine, logging: LoggingModule) -> None:
        self.logger = logging.getLogger(__name__)
        self.engine = engine
        self.locked_fields = {}
        self.additional_relation_models: ModelMap = defaultdict(dict)

    def retrieve(self, command: commands.Command) -> DatastoreResponse:
        """
        Uses engine to send data to datastore and retrieve result.

        This method also checks the payload and decodes JSON body.
        """
        content, status_code = self.engine.retrieve(command.name, command.data)
        if len(content):
            try:
                payload = json.loads(content)
            except JSONDecodeError:
                error_message = f"Bad response from datastore service. Body does not contain valid JSON. Received: {str(content)}"
                raise DatastoreException(error_message)
        else:
            payload = None
        self.logger.debug(f"Get response with status code {status_code}: {payload}")
        if status_code >= 400:
            error_message = f"Datastore service sends HTTP {status_code}."
            additional_error_message = (
                payload.get("error") if isinstance(payload, dict) else None
            )
            if additional_error_message is not None:
                type_verbose = additional_error_message.get("type_verbose")
                if type_verbose == "MODEL_LOCKED":
                    broken_locks = (
                        "'"
                        + "', '".join(sorted(additional_error_message.get("keys")))
                        + "'"
                    )
                    raise DatastoreLockedException(
                        " ".join(
                            (
                                error_message,
                                f"The following locks were broken: {broken_locks}",
                            )
                        )
                    )
                elif type_verbose == "MODEL_DOES_NOT_EXIST":
                    error_message = " ".join(
                        (
                            error_message,
                            f"Model '{additional_error_message.get('fqid')}' does not exist.",
                        )
                    )
                else:
                    error_message = " ".join(
                        (error_message, str(additional_error_message))
                    )
            raise DatastoreException(error_message)
        return payload

    def get(
        self,
        fqid: FullQualifiedId,
        mapped_fields: List[str] = None,
        position: int = None,
        get_deleted_models: DeletedModelsBehaviour = DeletedModelsBehaviour.NO_DELETED,
        lock_result: bool = True,
    ) -> PartialModel:
        mapped_fields_set = set()
        if mapped_fields:
            mapped_fields_set.update(mapped_fields)
            if lock_result:
                mapped_fields_set.add("meta_position")
        command = commands.Get(
            fqid=fqid,
            mapped_fields=mapped_fields_set,
            position=position,
            get_deleted_models=get_deleted_models,
        )
        self.logger.debug(
            f"Start GET request to datastore with the following data: {command.data}"
        )
        response = self.retrieve(command)
        if lock_result:
            instance_position = response.get("meta_position")
            if instance_position is None:
                raise DatastoreException(
                    "Response from datastore does not contain field 'meta_position' but this is required."
                )
            self.update_locked_fields_from_mapped_fields(
                fqid, instance_position, mapped_fields_set
            )
        return response

    def get_many(
        self,
        get_many_requests: List[commands.GetManyRequest],
        mapped_fields: List[str] = None,
        position: int = None,
        get_deleted_models: DeletedModelsBehaviour = DeletedModelsBehaviour.NO_DELETED,
        lock_result: bool = True,
    ) -> Dict[Collection, Dict[int, PartialModel]]:
        if mapped_fields is not None:
            raise NotImplementedError(
                "The keyword 'mapped_fields' is not supported. Please use mapped_fields inside the GetManyRequest."
            )
        if lock_result:
            for get_many_request in get_many_requests:
                if get_many_request.mapped_fields is not None:
                    get_many_request.mapped_fields.add("meta_position")

        command = commands.GetMany(
            get_many_requests=get_many_requests,
            mapped_fields=mapped_fields,
            position=position,
            get_deleted_models=get_deleted_models,
        )
        self.logger.debug(
            f"Start GET_MANY request to datastore with the following data: {command.data}"
        )
        response = self.retrieve(command)
        result: Dict[Collection, Dict[int, PartialModel]] = defaultdict(dict)
        for get_many_request in get_many_requests:
            collection = get_many_request.collection
            if collection.collection not in response:
                continue

            for instance_id in get_many_request.ids:
                if str(instance_id) not in response[collection.collection]:
                    continue
                value = response[collection.collection][str(instance_id)]
                if lock_result:
                    instance_position = value.get("meta_position")
                    if instance_position is None:
                        raise DatastoreException(
                            "Response from datastore does not contain field 'meta_position' but this is required."
                        )
                    fqid = FullQualifiedId(collection, instance_id)
                    self.update_locked_fields_from_mapped_fields(
                        fqid, instance_position, get_many_request.mapped_fields
                    )
                result[collection][instance_id] = value
        return result

    def get_all(
        self,
        collection: Collection,
        mapped_fields: List[str] = None,
        get_deleted_models: DeletedModelsBehaviour = DeletedModelsBehaviour.NO_DELETED,
        lock_result: bool = True,
    ) -> Dict[int, PartialModel]:
        mapped_fields_set = set()
        if mapped_fields:
            mapped_fields_set.update(mapped_fields)
            if lock_result:
                mapped_fields_set.update(("id", "meta_position"))
        command = commands.GetAll(
            collection=collection,
            mapped_fields=mapped_fields_set,
            get_deleted_models=get_deleted_models,
        )
        self.logger.debug(
            f"Start GET_ALL request to datastore with the following data: {command.data}"
        )
        response = self.retrieve(command)
        if lock_result and len(response) > 0:
            if not mapped_fields:
                raise DatastoreException(
                    "You cannot lock in get_all without mapped_fields"
                )
            for field in mapped_fields_set:
                # just take the first position, new positions will always be higher anyway
                instance_position = list(response.values())[0].get("meta_position")
                if instance_position is None:
                    raise DatastoreException(
                        "Response from datastore does not contain field 'meta_position' but this is required."
                    )
                collection_field = CollectionField(collection, field)
                self.update_locked_fields(collection_field, instance_position)
        return response

    def filter(
        self,
        collection: Collection,
        filter: Filter,
        mapped_fields: List[str] = [],
        get_deleted_models: DeletedModelsBehaviour = DeletedModelsBehaviour.NO_DELETED,
        lock_result: bool = True,
    ) -> Dict[int, PartialModel]:
        full_filter = self.apply_deleted_models_behaviour_to_filter(
            filter, get_deleted_models
        )
        command = commands.Filter(
            collection=collection, filter=full_filter, mapped_fields=set(mapped_fields)
        )
        self.logger.debug(
            f"Start FILTER request to datastore with the following data: {command.data}"
        )
        response = self.retrieve(command)
        pos = response["position"]
        data = response["data"]
        if lock_result:
            fields = []
            filter_visitor(filter, lambda fo: fields.append(fo.field))
            if "meeting_id" not in fields:
                self.logger.warning(
                    "Logging a collection field with a filter which does not contain meeting_id!"
                )
            for field in fields:
                cf = CollectionField(collection, field)
                self.update_locked_fields(cf, {"position": pos, "filter": full_filter})
        data = {int(key): val for key, val in data.items()}
        return data

    def exists(
        self,
        collection: Collection,
        filter: Filter,
        get_deleted_models: DeletedModelsBehaviour = DeletedModelsBehaviour.NO_DELETED,
        lock_result: bool = True,
    ) -> bool:
        full_filter = self.apply_deleted_models_behaviour_to_filter(
            filter, get_deleted_models
        )
        command = commands.Exists(collection=collection, filter=full_filter)
        self.logger.debug(
            f"Start EXISTS request to datastore with the following data: {command.data}"
        )
        response = self.retrieve(command)
        if lock_result:
            if (pos := response.get("position")) is None:
                raise DatastoreException("Invalid response from datastore.")
            filter_visitor(
                filter,
                lambda fo: self.update_locked_fields(
                    CollectionField(collection, fo.field), pos
                ),
            )
        return response["exists"]

    def count(
        self,
        collection: Collection,
        filter: Filter,
        get_deleted_models: DeletedModelsBehaviour = DeletedModelsBehaviour.NO_DELETED,
        lock_result: bool = True,
    ) -> int:
        full_filter = self.apply_deleted_models_behaviour_to_filter(
            filter, get_deleted_models
        )
        command = commands.Count(collection=collection, filter=full_filter)
        self.logger.debug(
            f"Start COUNT request to datastore with the following data: {command.data}"
        )
        response = self.retrieve(command)
        if lock_result:
            if (pos := response.get("position")) is None:
                raise DatastoreException("Invalid response from datastore.")
            filter_visitor(
                filter,
                lambda fo: self.update_locked_fields(
                    CollectionField(collection, fo.field), pos
                ),
            )
        return response["count"]

    def min(
        self,
        collection: Collection,
        filter: Filter,
        field: str,
        type: str = "int",
        get_deleted_models: DeletedModelsBehaviour = DeletedModelsBehaviour.NO_DELETED,
        lock_result: bool = True,
    ) -> Optional[int]:
        full_filter = self.apply_deleted_models_behaviour_to_filter(
            filter, get_deleted_models
        )
        command = commands.Min(
            collection=collection, filter=full_filter, field=field, type=type
        )
        self.logger.debug(
            f"Start MIN request to datastore with the following data: {command.data}"
        )
        response = self.retrieve(command)
        if lock_result:
            if (pos := response.get("position")) is None:
                raise DatastoreException("Invalid response from datastore.")
            self.update_locked_fields(CollectionField(collection, field), pos)
            filter_visitor(
                filter,
                lambda fo: self.update_locked_fields(
                    CollectionField(collection, fo.field), pos
                ),
            )
        return response.get("min")

    def max(
        self,
        collection: Collection,
        filter: Filter,
        field: str,
        type: str = "int",
        get_deleted_models: DeletedModelsBehaviour = DeletedModelsBehaviour.NO_DELETED,
        lock_result: bool = True,
    ) -> Optional[int]:
        # TODO: This method does not reflect the position of the fetched objects.
        full_filter = self.apply_deleted_models_behaviour_to_filter(
            filter, get_deleted_models
        )
        command = commands.Max(
            collection=collection, filter=full_filter, field=field, type=type
        )
        self.logger.debug(
            f"Start MAX request to datastore with the following data: {command.data}"
        )
        response = self.retrieve(command)
        if lock_result:
            if (pos := response.get("position")) is None:
                raise DatastoreException("Invalid response from datastore.")
            self.update_locked_fields(CollectionField(collection, field), pos)
            filter_visitor(
                filter,
                lambda fo: self.update_locked_fields(
                    CollectionField(collection, fo.field), pos
                ),
            )
        return response.get("max")

    def apply_deleted_models_behaviour_to_filter(
        self, filter: Filter, get_deleted_models: DeletedModelsBehaviour
    ) -> Filter:
        """
        Takes the given filter and wraps an AND-Filter based on the given
        DeletedModelsBehaviour around it.
        """
        if get_deleted_models == DeletedModelsBehaviour.ALL_MODELS:
            return filter

        deleted_models_filter = FilterOperator(
            "meta_deleted",
            "=",
            get_deleted_models == DeletedModelsBehaviour.ONLY_DELETED,
        )
        return And(filter, deleted_models_filter)

    def update_locked_fields_from_mapped_fields(
        self, fqid: FullQualifiedId, position: int, mapped_fields: Optional[Set[str]]
    ) -> None:
        if mapped_fields:
            for field in mapped_fields:
                if not field.startswith("meta_"):
                    self.update_locked_fields(
                        FullQualifiedField(fqid.collection, fqid.id, field), position
                    )
        else:
            self.update_locked_fields(fqid, position)

    def update_locked_fields(
        self,
        key: Union[FullQualifiedId, FullQualifiedField, CollectionField],
        lock: Union[int, CollectionFieldLockWithFilter],
    ) -> None:
        """
        Updates the locked_fields map by adding the new value for the given FQId or
        FQField. To work properly in case of retry/reread we have to accept the new value always.
        """
        if not isinstance(lock, int) and not isinstance(key, CollectionField):
            raise DatastoreException(
                "You can only lock collection fields with a filter"
            )
        new_value: CollectionFieldLock = lock
        if old_pos := self.locked_fields.get(str(key)):
            if isinstance(old_pos, int) and isinstance(lock, int):
                # keep the smaller position
                if old_pos <= lock:
                    return
            else:
                # if we currently have a position saved, transform it into a list with one entry
                if isinstance(old_pos, int):
                    old_pos = [{"position": old_pos}]
                elif not isinstance(old_pos, list):
                    old_pos = [old_pos]
                # add the new lock to the list
                if isinstance(lock, int):
                    new_value = old_pos + [{"position": lock}]
                else:
                    new_value = old_pos + [lock]
        self.locked_fields[str(key)] = new_value

    def reserve_ids(self, collection: Collection, amount: int) -> Sequence[int]:
        command = commands.ReserveIds(collection=collection, amount=amount)
        self.logger.debug(
            f"Start RESERVE_IDS request to datastore with the following data: "
            f"Collection: {collection}, Amount: {amount}"
        )
        response = self.retrieve(command)
        return response.get("ids")

    def reserve_id(self, collection: Collection) -> int:
        return self.reserve_ids(collection=collection, amount=1)[0]

    def write(self, write_requests: Union[List[WriteRequest], WriteRequest]) -> None:
        if isinstance(write_requests, WriteRequest):
            write_requests = [write_requests]
        command = commands.Write(write_requests=write_requests)
        self.logger.debug(
            f"Start WRITE request to datastore with the following data: "
            f"Write request: {write_requests}"
        )
        self.retrieve(command)

    def truncate_db(self) -> None:
        command = commands.TruncateDb()
        self.logger.debug("Start TRUNCATE_DB request to datastore")
        self.retrieve(command)

    def update_additional_models(
        self, fqid: FullQualifiedId, instance: Dict[str, Any], replace: bool = False
    ) -> None:
        """
        Adds or replaces the model identified by fqid in the additional models.
        Automatically adds missing id field.
        """
        if replace or isinstance(instance, DeletedModel):
            self.additional_relation_models[fqid] = instance
        else:
            self.additional_relation_models[fqid].update(instance)
        if "id" not in self.additional_relation_models[fqid]:
            self.additional_relation_models[fqid]["id"] = fqid.id

    def fetch_model(
        self,
        fqid: FullQualifiedId,
        mapped_fields: List[str],
        position: int = None,
        get_deleted_models: DeletedModelsBehaviour = DeletedModelsBehaviour.NO_DELETED,
        lock_result: bool = True,
        db_additional_relevance: InstanceAdditionalBehaviour = InstanceAdditionalBehaviour.ADDITIONAL_BEFORE_DBINST,
        exception: bool = True,
    ) -> Dict[str, Any]:
        """
        Uses the current additional_relation_models to fetch the given model.
        additional_relation_models serves as a kind of cache layer of all recently done
        changes - all updates to any model during the action are saved in there.
        The parameter db_additional_relevance defines what is searched first: the
        datastore or the additional models.

        Use this over the get method when in doubt.
        """
        datastore_exception: Optional[DatastoreException] = None

        def get_additional() -> Tuple[bool, Dict[str, Any]]:
            if fqid in self.additional_relation_models and (
                get_deleted_models == DeletedModelsBehaviour.ALL_MODELS
                or (
                    isinstance(self.additional_relation_models[fqid], DeletedModel)
                    == (get_deleted_models == DeletedModelsBehaviour.ONLY_DELETED)
                )
            ):
                complete = True
                if mapped_fields:
                    instance = {}
                    for field in mapped_fields:
                        if field in self.additional_relation_models[fqid]:
                            instance[field] = self.additional_relation_models[fqid][
                                field
                            ]
                        else:
                            complete = False
                else:
                    instance = self.additional_relation_models[fqid]
                return (complete, instance)
            else:
                return (False, {})

        def get_db() -> Tuple[bool, Dict[str, Any], Optional[DatastoreException]]:
            try:
                instance = self.get(
                    fqid,
                    mapped_fields=mapped_fields,
                    position=position,
                    get_deleted_models=get_deleted_models,
                    lock_result=lock_result,
                )
                return (
                    True,
                    instance,
                    None,
                )
            except DatastoreException as e:
                return False, {}, e if exception else None

        if db_additional_relevance in (
            InstanceAdditionalBehaviour.ONLY_ADDITIONAL,
            InstanceAdditionalBehaviour.ADDITIONAL_BEFORE_DBINST,
        ):
            complete, result = get_additional()
            okay = bool(result)
            if (
                not complete
                and db_additional_relevance
                == InstanceAdditionalBehaviour.ADDITIONAL_BEFORE_DBINST
            ):
                cache_okay = okay
                cache_result = result
                okay, result, datastore_exception = get_db()
                if okay:
                    result = {**result, **cache_result}
                elif cache_okay:
                    okay = True
                    result = cache_result
        else:
            okay, result, datastore_exception = get_db()
            if (
                not okay
                and db_additional_relevance
                == InstanceAdditionalBehaviour.DBINST_BEFORE_ADDITIONAL
            ):
                okay, result = get_additional()
        if not okay and exception:
            if datastore_exception:
                raise datastore_exception
            else:
                raise DatastoreException(f"{fqid} not found at all.")
        return result

    def reset(self) -> None:
        self.locked_fields = {}
        self.additional_relation_models.clear()
