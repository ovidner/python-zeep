import copy
import logging

from lxml import etree

from zeep import exceptions
from zeep.exceptions import UnexpectedElementError
from zeep.utils import qname_attr
from zeep.xsd.const import NotSet, xsi_ns
from zeep.xsd.context import XmlParserContext
from zeep.xsd.elements.base import Base
from zeep.xsd.utils import max_occurs_iter

logger = logging.getLogger(__name__)

__all__ = ['Element']


class Element(Base):
    def __init__(self, name, type_=None, min_occurs=1, max_occurs=1,
                 nillable=False, default=None, is_global=False, attr_name=None):

        if name is None:
            raise ValueError("name cannot be None", self.__class__)
        if not isinstance(name, etree.QName):
            name = etree.QName(name)

        self.name = name.localname if name else None
        self.qname = name
        self.type = type_
        self.min_occurs = min_occurs
        self.max_occurs = max_occurs
        self.nillable = nillable
        self.is_global = is_global
        self.default = default
        self.attr_name = attr_name or self.name
        # assert type_

    def __str__(self):
        if self.type:
            if self.type.is_global:
                return '%s(%s)' % (self.name, self.type.qname)
            else:
                return '%s(%s)' % (self.name, self.type.signature())
        return '%s()' % self.name

    def __call__(self, *args, **kwargs):
        instance = self.type(*args, **kwargs)
        if hasattr(instance, '_xsd_type'):
            instance._xsd_elm = self
        return instance

    def __repr__(self):
        return '<%s(name=%r, type=%r)>' % (
            self.__class__.__name__, self.name, self.type)

    def __eq__(self, other):
        return (
            other is not None and
            self.__class__ == other.__class__ and
            self.__dict__ == other.__dict__)

    @property
    def default_value(self):
        value = [] if self.accepts_multiple else self.default
        return value

    def clone(self, name=None, min_occurs=1, max_occurs=1):
        new = copy.copy(self)

        if name:
            if not isinstance(name, etree.QName):
                name = etree.QName(name)
            new.name = name.localname
            new.qname = name
            new.attr_name = new.name

        new.min_occurs = min_occurs
        new.max_occurs = max_occurs
        return new

    def parse(self, xmlelement, schema, allow_none=False, context=None):
        """Process the given xmlelement. If it has an xsi:type attribute then
        use that for further processing. This should only be done for subtypes
        of the defined type but for now we just accept everything.

        """
        context = context or XmlParserContext()
        instance_type = qname_attr(xmlelement, xsi_ns('type'))
        xsd_type = None
        if instance_type:
            xsd_type = schema.get_type(instance_type, fail_silently=True)
        xsd_type = xsd_type or self.type
        return xsd_type.parse_xmlelement(
            xmlelement, schema, allow_none=allow_none, context=context)

    def parse_kwargs(self, kwargs, name, available_kwargs):
        return self.type.parse_kwargs(
            kwargs, name or self.attr_name, available_kwargs)

    def parse_xmlelements(self, xmlelements, schema, name=None, context=None):
        """Consume matching xmlelements and call parse() on each of them"""
        result = []
        num_matches = 0
        for _unused in max_occurs_iter(self.max_occurs):
            if not xmlelements:
                break

            # Workaround for SOAP servers which incorrectly use unqualified
            # or qualified elements in the responses (#170, #176). To make the
            # best of it we compare the full uri's if both elements have a
            # namespace. If only one has a namespace then only compare the
            # localname.

            # If both elements have a namespace and they don't match then skip
            element_tag = etree.QName(xmlelements[0].tag)
            if (
                element_tag.namespace and self.qname.namespace and
                element_tag.namespace != self.qname.namespace
            ):
                break

            # Only compare the localname
            if element_tag.localname == self.qname.localname:
                xmlelement = xmlelements.popleft()
                num_matches += 1
                item = self.parse(
                    xmlelement, schema, allow_none=True, context=context)
                if item is not None:
                    result.append(item)
            else:
                # If the element passed doesn't match and the current one is
                # not optional then throw an error
                if num_matches == 0 and not self.is_optional:
                    raise UnexpectedElementError(
                        "Unexpected element %r, expected %r" % (
                            element_tag.text, self.qname.text))
                break

        if not self.accepts_multiple:
            result = result[0] if result else None
        return result

    def render(self, parent, value, render_path=None):
        """Render the value(s) on the parent lxml.Element.

        This actually just calls _render_value_item for each value.

        """
        if not render_path:
            render_path = [self.qname.localname]

        assert parent is not None
        self.validate(value, render_path)

        if self.accepts_multiple and isinstance(value, list):
            for val in value:
                self._render_value_item(parent, val, render_path)
        else:
            self._render_value_item(parent, value, render_path)

    def _render_value_item(self, parent, value, render_path):
        """Render the value on the parent lxml.Element"""
        if value is None or value is NotSet:
            if self.is_optional:
                return

            elm = etree.SubElement(parent, self.qname)
            if self.nillable:
                elm.set(xsi_ns('nil'), 'true')
            return

        node = etree.SubElement(parent, self.qname)
        xsd_type = getattr(value, '_xsd_type', self.type)

        if xsd_type != self.type:
            return value._xsd_type.render(node, value, xsd_type, render_path)
        return self.type.render(node, value, None, render_path)

    def validate(self, value, render_path=None):
        """Validate that the value is valid"""
        if self.accepts_multiple and isinstance(value, list):

            # Validate bounds
            if len(value) < self.min_occurs:
                raise exceptions.ValidationError(
                    "Expected at least %d items (minOccurs check)" % self.min_occurs,
                    path=render_path)
            elif self.max_occurs != 'unbounded' and len(value) > self.max_occurs:
                raise exceptions.ValidationError(
                    "Expected at most %d items (maxOccurs check)" % self.min_occurs,
                    path=render_path)

            for val in value:
                self._validate_item(val, render_path)
        else:
            if not self.is_optional and not self.nillable and value in (None, NotSet):
                raise exceptions.ValidationError(
                    "Missing element %s" % (self.name), path=render_path)

            self._validate_item(value, render_path)

    def _validate_item(self, value, render_path):
        if self.nillable and value in (None, NotSet):
            return

        try:
            self.type.validate(value, required=True)
        except exceptions.ValidationError as exc:
            raise exceptions.ValidationError(
                "The element %s is not valid: %s" % (self.qname, exc.message),
                path=render_path)

    def resolve_type(self):
        self.type = self.type.resolve()

    def resolve(self):
        self.resolve_type()
        return self

    def signature(self, depth=()):
        if len(depth) > 0 and self.is_global:
            return self.name + '()'

        if self.type.is_global:
            value = self.type.qname
        else:
            value = self.type.signature(depth)
        if self.accepts_multiple:
            return '%s[]' % value
        return value
