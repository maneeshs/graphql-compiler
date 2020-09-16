# Copyright 2019-present Kensho Technologies, LLC.
from copy import copy
import string
from typing import Any, Dict, FrozenSet, List, Optional, Set, Type, TypeVar, Union

from graphql import GraphQLSchema, build_ast_schema, specified_scalar_types
from graphql.language.ast import (
    DirectiveNode,
    DocumentNode,
    EnumTypeDefinitionNode,
    FieldDefinitionNode,
    FieldNode,
    FragmentSpreadNode,
    InlineFragmentNode,
    InterfaceTypeDefinitionNode,
    NamedTypeNode,
    NameNode,
    Node,
    ObjectTypeDefinitionNode,
    ScalarTypeDefinitionNode,
    SelectionSetNode,
    UnionTypeDefinitionNode,
)
from graphql.language.visitor import Visitor, visit
from graphql.type.definition import GraphQLScalarType
from graphql.utilities.assert_valid_name import re_name
from graphql.validation import validate
import six

from ..ast_manipulation import get_ast_with_non_null_and_list_stripped
from ..exceptions import GraphQLError, GraphQLValidationError
from ..schema import FilterDirective, OptionalDirective, OutputDirective


class SchemaTransformError(GraphQLError):
    """Parent of specific error classes."""


class SchemaStructureError(SchemaTransformError):
    """Raised if an input schema's structure is illegal.

    This may happen if an AST cannot be built into a schema, if the schema contains disallowed
    components, or if the schema contains some field of the query type that is named differently
    from the type it queries.
    """


class InvalidTypeNameError(SchemaTransformError):
    """Raised if a type/field name is not valid.

    This may be raised if the input schema contains invalid names, or if the user attempts to
    rename a type/field to an invalid name. A name is considered valid if it consists of
    alphanumeric characters and underscores and doesn't start with a numeric character (as
    required by GraphQL), and doesn't start with double underscores as such type names are
    reserved for GraphQL internal use.
    """


class SchemaMergeNameConflictError(SchemaTransformError):
    """Raised when merging types or fields cause name conflicts.

    This may be raised if two merged schemas share an identically named field or type, or if a
    CrossSchemaEdgeDescriptor provided when merging schemas has an edge name that causes a
    name conflict with an existing field.
    """


class SchemaRenameNameConflictError(SchemaTransformError):
    """Raised when renaming causes name conflicts."""

    name_conflicts: Dict[str, Set[str]]
    renamed_to_builtin_scalar_conflicts: Dict[str, str]

    def __init__(
        self,
        name_conflicts: Dict[str, Set[str]],
        renamed_to_builtin_scalar_conflicts: Dict[str, str],
    ) -> None:
        """Record all renaming conflicts."""
        if not name_conflicts and not renamed_to_builtin_scalar_conflicts:
            raise ValueError(
                "Cannot raise SchemaRenameNameConflictError without at least one conflict, but "
                "name_conflicts and renamed_to_builtin_scalar_conflicts arguments were both empty "
                "dictionaries."
            )
        super().__init__()
        self.name_conflicts = name_conflicts
        self.renamed_to_builtin_scalar_conflicts = renamed_to_builtin_scalar_conflicts

    def __str__(self) -> str:
        """Explain renaming conflict and the fix."""
        name_conflicts_message = ""
        if self.name_conflicts:
            name_conflicts_message = (
                f"Applying the renaming would produce a schema in which multiple types have the "
                f"same name, which is an illegal schema state. The name_conflicts dict describes "
                f"these problems. For each key k in name_conflicts, name_conflicts[k] is the set "
                f"of types in the original schema that get mapped to k in the new schema. To fix "
                f"this, modify the renamings argument of rename_schema to ensure that no two types "
                f"in the renamed schema have the same name. name_conflicts: {self.name_conflicts}"
            )
        renamed_to_builtin_scalar_conflicts_message = ""
        if self.renamed_to_builtin_scalar_conflicts:
            renamed_to_builtin_scalar_conflicts_message = (
                f"Applying the renaming would rename type(s) to a name already used by a built-in "
                f"GraphQL scalar type. To fix this, ensure that no type name is mapped to a "
                f"scalar's name. The following dict maps each to-be-renamed type to the scalar "
                f"name it was mapped to: {self.renamed_to_builtin_scalar_conflicts}"
            )
        return "\n".join(
            filter(None, [name_conflicts_message, renamed_to_builtin_scalar_conflicts_message])
        )


class InvalidCrossSchemaEdgeError(SchemaTransformError):
    """Raised when a CrossSchemaEdge provided when merging schemas is invalid.

    This may be raised if the provided CrossSchemaEdge refers to nonexistent schemas,
    types not found in the specified schema, or fields not found in the specified type.
    """


class CascadingSuppressionError(SchemaTransformError):
    """Raised if existing suppressions would require further suppressions.

    This may be raised during schema renaming if it:
    * suppresses all the fields of a type but not the type itself
    * suppresses all the members of a union but not the union itself
    * suppresses a type X but there still exists a different type Y that has fields of type X.
    The error message will suggest fixing this illegal state by describing further suppressions, but
    adding these suppressions may lead to other types, unions, fields, etc. needing suppressions of
    their own. Most real-world schemas wouldn't have these cascading situations, and if they do,
    they are unlikely to have many of them, so the error messages are not meant to describe the full
    sequence of steps required to fix all suppression errors in one pass.
    """


_alphanumeric_and_underscore: FrozenSet[str] = frozenset(
    six.text_type(string.ascii_letters + string.digits + "_")
)


# String representations for the GraphQL built-in scalar types
# pylint produces a false positive-- see issue here: https://github.com/PyCQA/pylint/issues/3743
builtin_scalar_type_names: FrozenSet[str] = frozenset(
    specified_scalar_types.keys()  # pylint: disable=no-member
)


# Union of classes of nodes to be renamed or suppressed by an instance of RenameSchemaTypesVisitor.
# Note that RenameSchemaTypesVisitor also has a class attribute rename_types which parallels the
# classes here. This duplication is necessary due to language and linter constraints-- see the
# comment in the RenameSchemaTypesVisitor class for more information.
# Unfortunately, RenameTypes itself has to be a module attribute instead of a class attribute
# because a bug in flake8 produces a linting error if RenameTypes is a class attribute and we type
# hint the return value of the RenameSchemaTypesVisitor's
# _rename_or_suppress_or_ignore_name_and_add_to_record() method as RenameTypes. More on this here:
# https://github.com/PyCQA/pyflakes/issues/441
RenameTypes = Union[
    EnumTypeDefinitionNode,
    InterfaceTypeDefinitionNode,
    NamedTypeNode,
    ObjectTypeDefinitionNode,
    UnionTypeDefinitionNode,
]
RenameTypesT = TypeVar("RenameTypesT", bound=RenameTypes)

# For the same reason as with RenameTypes, these types have to be written out explicitly instead of
# relying on allowed_types in get_copy_of_node_with_new_name.
# Unlike RenameTypes, RenameNodes also includes fields because it's used in the function
# get_copy_of_node_with_new_name which rename_query depends on to rename the root field in a query.
# Meanwhile, RenameTypes applies only for rename_schema and field renaming in the schema is not
# implemented yet.
RenameNodes = Union[
    RenameTypes,
    FieldNode,
    FieldDefinitionNode,
]
RenameNodesT = TypeVar("RenameNodesT", bound=RenameNodes)


def check_schema_identifier_is_valid(identifier: str) -> None:
    """Check if input is a valid identifier, made of alphanumeric and underscore characters.

    Args:
        identifier: str, used for identifying input schemas when merging multiple schemas

    Raises:
        - ValueError if the name is the empty string, or if it consists of characters other
          than alphanumeric characters and underscores
    """
    if not isinstance(identifier, str):
        raise ValueError('Schema identifier "{}" is not a string.'.format(identifier))
    if identifier == "":
        raise ValueError("Schema identifier must be a nonempty string.")
    illegal_characters = frozenset(identifier) - _alphanumeric_and_underscore
    if illegal_characters:
        raise ValueError(
            'Schema identifier "{}" contains illegal characters: {}'.format(
                identifier, illegal_characters
            )
        )


def type_name_is_valid(name: str) -> bool:
    """Check if input is a valid, nonreserved GraphQL type name.

    A GraphQL type name is valid iff it consists of only alphanumeric characters and underscores and
    does not start with a numeric character. It is nonreserved (i.e. not reserved for GraphQL
    internal use) if it does not start with double underscores.

    Args:
        name: to be checked

    Returns:
        True iff name is a valid, nonreserved GraphQL type name.
    """
    return bool(re_name.match(name)) and not name.startswith("__")


def get_query_type_name(schema: GraphQLSchema) -> str:
    """Get the name of the query type of the input schema (e.g. RootSchemaQuery)."""
    if schema.query_type is None:
        raise AssertionError(
            "Schema's query_type field is None, even though the compiler is read-only."
        )
    return schema.query_type.name


def get_custom_scalar_names(schema: GraphQLSchema) -> Set[str]:
    """Get names of all custom scalars used in the input schema.

    Includes all user defined scalars; excludes builtin scalars.

    Note: If the user defined a scalar that shares its name with a builtin introspection type
    (such as __Schema, __Directive, etc), it will not be listed in type_map and thus will not
    be included in the output.

    Returns:
        set of names of scalars used in the schema
    """
    type_map = schema.type_map
    custom_scalar_names = {
        type_name
        for type_name, type_object in six.iteritems(type_map)
        if isinstance(type_object, GraphQLScalarType) and type_name not in builtin_scalar_type_names
    }
    return custom_scalar_names


def try_get_ast_by_name_and_type(
    asts: Optional[List[Node]], target_name: str, target_type: Type[Node]
) -> Optional[Node]:
    """Return the ast in the list with the desired name and type, if found.

    Args:
        asts: optional list of asts to search through
        target_name: name of the AST we're looking for
        target_type: type of the AST we're looking for. Instances of this type must have a .name
                     attribute, (e.g. FieldNode, DirectiveNode) and its .name attribute must have a
                     .value attribute.

    Returns:
        element in the input list with the correct name and type, or None if not found
    """
    if asts is None:
        return None
    for ast in asts:
        if isinstance(ast, target_type):
            if not (hasattr(ast, "name") and hasattr(ast.name, "value")):  # type: ignore
                # Can't type hint "has .name attribute"
                raise AssertionError(
                    f"AST {ast} is either missing a .name attribute or its .name attribute is "
                    f"missing a .value attribute. This should be impossible because target_type "
                    f"{target_type} must have a .name attribute, {target_type}'s .name attribute "
                    f"must have a .value attribute, and the ast must be of type {target_type}."
                )
            if ast.name.value == target_name:  # type: ignore
                # Can't type hint "has .name attribute"
                return ast
    return None


def try_get_inline_fragment(
    selections: Optional[List[Union[FieldNode, InlineFragmentNode]]]
) -> Optional[InlineFragmentNode]:
    """Return the unique inline fragment contained in selections, or None.

    Args:
        selections: optional list of selections to search through

    Returns:
        inline fragment if one is found in selections, None otherwise

    Raises:
        GraphQLValidationError if selections contains a InlineFragment along with a nonzero
        number of fields, or contains multiple InlineFragments
    """
    if selections is None:
        return None
    inline_fragments_in_selection = [
        selection for selection in selections if isinstance(selection, InlineFragmentNode)
    ]
    if len(inline_fragments_in_selection) == 0:
        return None
    elif len(inline_fragments_in_selection) == 1:
        if len(selections) == 1:
            return inline_fragments_in_selection[0]
        else:
            raise GraphQLValidationError(
                'Input selections "{}" contains both InlineFragments and Fields, which may not '
                "coexist in one selection.".format(selections)
            )
    else:
        raise GraphQLValidationError(
            'Input selections "{}" contains multiple InlineFragments, which is not allowed.'
            "".format(selections)
        )


def get_copy_of_node_with_new_name(node: RenameNodesT, new_name: str) -> RenameNodesT:
    """Return a node with new_name as its name and otherwise identical to the input node.

    Args:
        node: node to make a copy of
        new_name: name to give to the output node

    Returns:
        node with new_name as its name and otherwise identical to the input node
    """
    node_type = type(node).__name__
    allowed_types = frozenset(
        (
            "EnumTypeDefinitionNode",
            "FieldNode",
            "FieldDefinitionNode",
            "InterfaceTypeDefinitionNode",
            "NamedTypeNode",
            "ObjectTypeDefinitionNode",
            "UnionTypeDefinitionNode",
        )
    )
    if node_type not in allowed_types:
        raise AssertionError(
            "Input node {} of type {} is not allowed, only {} are allowed.".format(
                node, node_type, allowed_types
            )
        )
    node_with_new_name = copy(node)  # shallow copy is enough
    node_with_new_name.name = NameNode(value=new_name)
    return node_with_new_name


class CheckValidTypesAndNamesVisitor(Visitor):
    """Check that the AST does not contain invalid types or types with invalid names.

    If AST contains invalid types, raise SchemaStructureError; if AST contains types with
    invalid names, raise InvalidTypeNameError.
    """

    disallowed_types = frozenset(
        {  # types not supported in renaming or merging
            "InputObjectTypeDefinitionNode",
            "ObjectTypeExtensionNode",
        }
    )
    unexpected_types = frozenset(
        {  # types not expected to be found in schema definition
            "FieldNode",
            "FragmentDefinitionNode",
            "FragmentSpreadNode",
            "InlineFragmentNode",
            "ObjectFieldNode",
            "ObjectValueNode",
            "OperationDefinitionNode",
            "SelectionSetNode",
            "VariableNode",
            "VariableDefinitionNode",
        }
    )
    check_name_validity_types = (
        EnumTypeDefinitionNode,
        InterfaceTypeDefinitionNode,
        ObjectTypeDefinitionNode,
        ScalarTypeDefinitionNode,
        UnionTypeDefinitionNode,
    )

    def enter(
        self, node: Node, key: Any, parent: Any, path: List[Any], ancestors: List[Any]
    ) -> None:
        """Raise error if node is of a invalid type or has an invalid name.

        Raises:
            - SchemaStructureError if the node is an InputObjectTypeDefinition,
              TypeExtensionDefinition, or a type that shouldn't exist in a schema definition
            - InvalidTypeNameError if a node has an invalid name
        """
        node_type = type(node).__name__
        if node_type in self.disallowed_types:
            raise SchemaStructureError('Node type "{}" not allowed.'.format(node_type))
        elif node_type in self.unexpected_types:
            raise SchemaStructureError('Node type "{}" unexpected in schema AST'.format(node_type))
        elif isinstance(node, self.check_name_validity_types):
            if not type_name_is_valid(node.name.value):
                raise InvalidTypeNameError(
                    f"Node name {node.name.value} is not a valid, unreserved GraphQL name. Valid, "
                    f"unreserved GraphQL names must consist of only alphanumeric characters and "
                    f"underscores, must not start with a numeric character, and must not start "
                    f"with double underscores."
                )


class CheckQueryTypeFieldsNameMatchVisitor(Visitor):
    """Check that every query type field's name is identical to the type it queries.

    If not, raise SchemaStructureError.
    """

    def __init__(self, query_type: str) -> None:
        """Create a visitor for checking query type field names.

        Args:
            query_type: name of the query type (e.g. RootSchemaQuery)
        """
        self.query_type = query_type
        self.in_query_type = False

    def enter_object_type_definition(
        self,
        node: ObjectTypeDefinitionNode,
        key: Any,
        parent: Any,
        path: List[Any],
        ancestors: List[Any],
    ) -> None:
        """If the node's name matches the query type, record that we entered the query type."""
        if node.name.value == self.query_type:
            self.in_query_type = True

    def leave_object_type_definition(
        self,
        node: ObjectTypeDefinitionNode,
        key: Any,
        parent: Any,
        path: List[Any],
        ancestors: List[Any],
    ) -> None:
        """If the node's name matches the query type, record that we left the query type."""
        if node.name.value == self.query_type:
            self.in_query_type = False

    def enter_field_definition(
        self,
        node: FieldDefinitionNode,
        key: Any,
        parent: Any,
        path: List[Any],
        ancestors: List[Any],
    ) -> None:
        """If inside the query type, check that the field and queried type names match.

        Raises:
            - SchemaStructureError if the field name is not identical to the name of the type
              that it queries
        """
        if self.in_query_type:
            field_name = node.name.value
            type_node = get_ast_with_non_null_and_list_stripped(node.type)
            queried_type_name = type_node.name.value
            if field_name != queried_type_name:
                raise SchemaStructureError(
                    'Query type\'s field name "{}" does not match corresponding queried type '
                    'name "{}"'.format(field_name, queried_type_name)
                )


def check_ast_schema_is_valid(ast: DocumentNode) -> None:
    """Check the schema satisfies structural requirements for rename and merge.

    In particular, check that the schema contains no mutations, no subscriptions, no
    InputObjectTypeDefinitions, no TypeExtensionDefinitions, all type names are valid and not
    reserved (not starting with double underscores), and all query type field names match the
    types they query.

    Args:
        ast: represents schema

    Raises:
        - SchemaStructureError if the AST cannot be built into a valid schema, if the schema
          contains mutations, subscriptions, InputObjectTypeDefinitions, TypeExtensionsDefinitions,
          or if any query type field does not match the queried type.
        - InvalidTypeNameError if a type has a type name that is invalid or reserved
    """
    schema = build_ast_schema(ast)

    if schema.mutation_type is not None:
        raise SchemaStructureError(
            "Renaming schemas that contain mutations is currently not supported."
        )
    if schema.subscription_type is not None:
        raise SchemaStructureError(
            "Renaming schemas that contain subscriptions is currently not supported."
        )

    visit(ast, CheckValidTypesAndNamesVisitor())

    query_type = get_query_type_name(schema)
    visit(ast, CheckQueryTypeFieldsNameMatchVisitor(query_type))


def is_property_field_ast(field: FieldNode) -> bool:
    """Return True iff selection is a property field (i.e. no further selections)."""
    if isinstance(field, FieldNode):
        # Unfortunately, since split_query.py hasn't been type-hinted yet, we can't rely on the
        # type-hint in this function to ensure field is a FieldNode yet.
        return (
            field.selection_set is None
            or field.selection_set.selections is None
            or field.selection_set.selections == []
        )
    else:
        raise AssertionError('Input selection "{}" is not a Field.'.format(field))


class CheckQueryIsValidToSplitVisitor(Visitor):
    """Check the query is valid.

    In particular, check that it only contains supported directives, its property fields come
    before vertex fields in every scope, and that any scope containing a InlineFragment has
    nothing else in scope.
    """

    # This is very restrictive for now. Other cases (e.g. tags not crossing boundaries) are
    # also ok, but temporarily not allowed
    supported_directives = frozenset(
        (
            FilterDirective.name,
            OutputDirective.name,
            OptionalDirective.name,
        )
    )

    def enter_directive(
        self, node: DirectiveNode, key: Any, parent: Any, path: List[Any], ancestors: List[Any]
    ) -> None:
        """Check that the directive is supported."""
        if node.name.value not in self.supported_directives:
            raise GraphQLValidationError(
                'Directive "{}" is not yet supported, only "{}" are currently '
                "supported.".format(node.name.value, self.supported_directives)
            )

    def enter_selection_set(
        self, node: SelectionSetNode, key: Any, parent: Any, path: List[Any], ancestors: List[Any]
    ) -> None:
        """Check selections are valid.

        If selections contains an InlineFragment, check that it is the only inline fragment in
        scope. Otherwise, check that property fields occur before vertex fields.

        Args:
            node: selection set
            key: The index or key to this node from the parent node or Array.
            parent: the parent immediately above this node, which may be an Array.
            path: The key path to get to this node from the root node.
            ancestors: All nodes and Arrays visited before reaching parent of this node. These
                       correspond to array indices in ``path``. Note: ancestors includes arrays
                       which contain the parent of visited node.
        """
        selections = node.selections
        if len(selections) == 1 and isinstance(selections[0], InlineFragmentNode):
            return
        else:
            seen_vertex_field = False  # Whether we're seen a vertex field
            for field in selections:
                if isinstance(field, InlineFragmentNode):
                    raise GraphQLValidationError(
                        "Inline fragments must be the only selection in scope. However, in "
                        "selections {}, an InlineFragment coexists with other selections.".format(
                            selections
                        )
                    )
                if isinstance(field, FragmentSpreadNode):
                    raise GraphQLValidationError(
                        f"Fragments (not to be confused with inline fragments) are not supported "
                        f"by the compiler. However, in SelectionSetNode {node}'s selections "
                        f"attribute {selections}, the field {field} is a FragmentSpreadNode named "
                        f"{field.name.value}."
                    )
                if not isinstance(field, FieldNode):
                    raise AssertionError(
                        f"The SelectionNode {field} in SelectionSetNode {node}'s selections "
                        f"attribute is not a FieldNode but instead has type {type(field)}."
                    )
                if is_property_field_ast(field):
                    if seen_vertex_field:
                        raise GraphQLValidationError(
                            "In the selections {}, the property field {} occurs after a vertex "
                            "field or a type coercion statement, which is not allowed, as all "
                            "property fields must appear before all vertex fields.".format(
                                node.selections, field
                            )
                        )
                else:
                    seen_vertex_field = True


def check_query_is_valid_to_split(schema: GraphQLSchema, query_ast: DocumentNode) -> None:
    """Check the query is valid for splitting.

    In particular, ensure that the query validates against the schema, does not contain
    unsupported directives, and that in each selection, all property fields occur before all
    vertex fields.

    Args:
        schema: schema the query is written against
        query_ast: query to split

    Raises:
        GraphQLValidationError if the query doesn't validate against the schema, contains
        unsupported directives, or some property field occurs after a vertex field in some
        selection
    """
    # Check builtin errors
    built_in_validation_errors = validate(schema, query_ast)
    if len(built_in_validation_errors) > 0:
        raise GraphQLValidationError("AST does not validate: {}".format(built_in_validation_errors))
    # Check no bad directives and fields are in order
    visitor = CheckQueryIsValidToSplitVisitor()
    visit(query_ast, visitor)
