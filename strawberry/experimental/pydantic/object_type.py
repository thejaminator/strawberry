from __future__ import annotations

import dataclasses
import warnings
from functools import partial
from typing import (
    TYPE_CHECKING,
    Any,
    Callable,
    Dict,
    List,
    Optional,
    Sequence,
    Set,
    Type,
    cast,
)

from pydantic.fields import ModelField

from graphql import GraphQLResolveInfo

import strawberry
from strawberry.arguments import UNSET
from strawberry.experimental.pydantic.conversion import (
    convert_pydantic_model_to_strawberry_class,
    convert_strawberry_class_to_pydantic_model,
)
from strawberry.experimental.pydantic.exceptions import MissingFieldsListError
from strawberry.experimental.pydantic.fields import replace_types_recursively
from strawberry.experimental.pydantic.utils import (
    DataclassCreationFields,
    ensure_all_auto_fields_in_pydantic,
    get_default_factory_for_field,
    get_private_fields,
    sort_creation_fields,
)
from strawberry.field import StrawberryField
from strawberry.object_type import _process_type, _wrap_dataclass
from strawberry.schema_directive import StrawberrySchemaDirective
from strawberry.types.type_resolver import _get_fields


def get_type_for_field(field: ModelField, is_input: bool):
    outer_type = field.outer_type_
    replaced_type = replace_types_recursively(outer_type, is_input)

    if not field.required:
        return Optional[replaced_type]
    else:
        return replaced_type


def _build_dataclass_creation_fields(
    field: ModelField,
    is_input: bool,
    existing_fields: Dict[str, StrawberryField],
    auto_fields_set: Set[str],
    use_pydantic_alias: bool,
) -> DataclassCreationFields:
    type_annotation = (
        get_type_for_field(field, is_input)
        if field.name in auto_fields_set
        else existing_fields[field.name].type
    )

    if (
        field.name in existing_fields
        and existing_fields[field.name].base_resolver is not None
    ):
        # if the user has defined a resolver for this field, always use it
        strawberry_field = existing_fields[field.name]
    else:
        # otherwise we build an appropriate strawberry field that resolves it
        existing_field = existing_fields.get(field.name)
        strawberry_field = StrawberryField(
            python_name=field.name,
            graphql_name=field.alias
            if field.has_alias and use_pydantic_alias
            else None,
            # always unset because we use default_factory instead
            default=UNSET,
            default_factory=get_default_factory_for_field(field),
            type_annotation=type_annotation,
            description=field.field_info.description,
            deprecation_reason=(
                existing_field.deprecation_reason if existing_field else None
            ),
            permission_classes=(
                existing_field.permission_classes if existing_field else []
            ),
            directives=existing_field.directives if existing_field else (),
        )

    return DataclassCreationFields(
        name=field.name,
        type_annotation=type_annotation,
        field=strawberry_field,
    )


if TYPE_CHECKING:
    from strawberry.experimental.pydantic.conversion_types import (
        PydanticModel,
        StrawberryTypeFromPydantic,
    )


def type(
    model: Type[PydanticModel],
    *,
    fields: Optional[List[str]] = None,
    name: Optional[str] = None,
    is_input: bool = False,
    is_interface: bool = False,
    description: Optional[str] = None,
    directives: Optional[Sequence[StrawberrySchemaDirective]] = (),
    all_fields: bool = False,
    use_pydantic_alias: bool = True,
) -> Callable[..., Type[StrawberryTypeFromPydantic[PydanticModel]]]:
    def wrap(cls: Any) -> Type[StrawberryTypeFromPydantic[PydanticModel]]:
        model_fields = model.__fields__
        original_fields_set = set(fields) if fields else set([])

        if fields:
            warnings.warn(
                "`fields` is deprecated, use `auto` type annotations instead",
                DeprecationWarning,
            )

        existing_fields = getattr(cls, "__annotations__", {})
        # these are the fields that matched a field name in the pydantic model
        # and should copy their alias from the pydantic model
        fields_set = original_fields_set.union(
            set(name for name, _ in existing_fields.items() if name in model_fields)
        )
        # these are the fields that were marked with strawberry.auto and
        # should copy their type from the pydantic model
        auto_fields_set = original_fields_set.union(
            set(name for name, typ in existing_fields.items() if typ == strawberry.auto)
        )

        if all_fields:
            if fields_set:
                warnings.warn(
                    "Using all_fields overrides any explicitly defined fields "
                    "in the model, using both is likely a bug",
                    stacklevel=2,
                )
            fields_set = set(model_fields.keys())
            auto_fields_set = set(model_fields.keys())

        if not fields_set:
            raise MissingFieldsListError(cls)

        ensure_all_auto_fields_in_pydantic(
            model=model, auto_fields=auto_fields_set, cls_name=cls.__name__
        )

        wrapped = _wrap_dataclass(cls)
        extra_strawberry_fields = _get_fields(wrapped)
        extra_fields = cast(List[dataclasses.Field], extra_strawberry_fields)
        private_fields = get_private_fields(wrapped)

        extra_fields_dict = {field.name: field for field in extra_strawberry_fields}

        all_model_fields: List[DataclassCreationFields] = [
            _build_dataclass_creation_fields(
                field, is_input, extra_fields_dict, auto_fields_set, use_pydantic_alias
            )
            for field_name, field in model_fields.items()
            if field_name in fields_set
        ]

        all_model_fields.extend(
            (
                DataclassCreationFields(
                    name=field.name,
                    type_annotation=field.type,
                    field=field,
                )
                for field in extra_fields + private_fields
                if field.name not in fields_set
            )
        )

        # Sort fields so that fields with missing defaults go first
        sorted_fields = sort_creation_fields(all_model_fields)

        # Implicitly define `is_type_of` to support interfaces/unions that use
        # pydantic objects (not the corresponding strawberry type)
        @classmethod  # type: ignore
        def is_type_of(cls: Type, obj: Any, _info: GraphQLResolveInfo) -> bool:
            return isinstance(obj, (cls, model))

        namespace = {"is_type_of": is_type_of}
        # We need to tell the difference between a from_pydantic method that is
        # inherited from a base class and one that is defined by the user in the
        # decorated class. We want to override the method only if it is
        # inherited. To tell the difference, we compare the class name to the
        # fully qualified name of the method, which will end in <class>.from_pydantic
        has_custom_from_pydantic = hasattr(
            cls, "from_pydantic"
        ) and cls.from_pydantic.__qualname__.endswith(f"{cls.__name__}.from_pydantic")
        has_custom_to_pydantic = hasattr(
            cls, "to_pydantic"
        ) and cls.to_pydantic.__qualname__.endswith(f"{cls.__name__}.to_pydantic")

        if has_custom_from_pydantic:
            namespace["from_pydantic"] = cls.from_pydantic
        if has_custom_to_pydantic:
            namespace["to_pydantic"] = cls.to_pydantic

        cls = dataclasses.make_dataclass(
            cls.__name__,
            [field.to_tuple() for field in sorted_fields],
            bases=cls.__bases__,
            namespace=namespace,
        )

        _process_type(
            cls,
            name=name,
            is_input=is_input,
            is_interface=is_interface,
            description=description,
            directives=directives,
        )

        if is_input:
            model._strawberry_input_type = cls  # type: ignore
        else:
            model._strawberry_type = cls  # type: ignore
        cls._pydantic_type = model

        def from_pydantic_default(
            instance: PydanticModel, extra: Dict[str, Any] = None
        ) -> StrawberryTypeFromPydantic[PydanticModel]:
            return convert_pydantic_model_to_strawberry_class(
                cls=cls, model_instance=instance, extra=extra
            )

        def to_pydantic_default(self) -> PydanticModel:
            instance_kwargs = {
                f.name: convert_strawberry_class_to_pydantic_model(
                    getattr(self, f.name)
                )
                for f in dataclasses.fields(self)
            }
            return model(**instance_kwargs)

        if not has_custom_from_pydantic:
            cls.from_pydantic = staticmethod(from_pydantic_default)
        if not has_custom_to_pydantic:
            cls.to_pydantic = to_pydantic_default

        return cls

    return wrap


input = partial(type, is_input=True)

interface = partial(type, is_interface=True)
