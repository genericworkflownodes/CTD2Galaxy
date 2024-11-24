#!/usr/bin/env python
import json
import logging
import ntpath
import operator
import os
import re
from functools import reduce

from CTDopts.CTDopts import (
    _InFile,
    _OutFile,
    CTDModel,
    ModelTypeError,
    Parameter,
    ParameterGroup,
    Parameters
)
from lxml import etree

from ..common.exceptions import ApplicationException


MESSAGE_INDENTATION_INCREMENT = 2


# simple struct-class containing a tuple with input/output location and the in-memory CTDModel
class ParsedCTD:
    def __init__(self, ctd_model=None, input_file=None, suggested_output_file=None):
        self.ctd_model = ctd_model
        self.input_file = input_file
        self.suggested_output_file = suggested_output_file


class ParameterHardcoder:
    def __init__(self):
        # map whose keys are the composite names of tools and parameters in the following pattern:
        # [ToolName][separator][ParameterName] -> HardcodedValue
        # if the parameter applies to all tools, then the following pattern is used:
        # [ParameterName] -> HardcodedValue

        # examples (assuming separator is '#'):
        # threads -> 24
        # XtandemAdapter#adapter -> xtandem.exe
        # adapter -> adapter.exe
        self.separator = "!"

        # hard coded values
        self.parameter_map = {}

        # ctd/xml attributes to overwrite
        self.attribute_map = {'CTD': {}, 'XML': {}}

        # blacklisted parameters
        self.blacklist = set()

    def register_blacklist(self, parameter_name, tool_name):
        k = self.build_key(parameter_name, tool_name)
        self.blacklist.add(k)

    # the most specific value will be returned in case of overlap
    def get_blacklist(self, parameter_name, tool_name):
        # look for the value that would apply for all tools
        if self.build_key(parameter_name, tool_name) in self.blacklist:
            return True
        elif parameter_name in self.blacklist:
            return True
        else:
            return False

    def register_attribute(self, parameter_name, attribute, value, tool_name):
        tpe, attribute = attribute.split(':')
        if tpe not in ['CTD', 'XML']:
            raise Exception('Attribute hardcoder not in CTD/XML')

        k = self.build_key(parameter_name, tool_name)
        if k not in self.attribute_map[tpe]:
            self.attribute_map[tpe][k] = {}
        self.attribute_map[tpe][k][attribute] = value

    # the most specific value will be returned in case of overlap
    def get_hardcoded_attributes(self, parameter_name, tool_name, tpe):
        # look for the value that would apply for all tools
        try:
            return self.attribute_map[tpe][self.build_key(parameter_name, tool_name)]
        except KeyError:
            return self.attribute_map[tpe].get(parameter_name, None)

    # the most specific value will be returned in case of overlap
    def get_hardcoded_value(self, parameter_name, tool_name):
        # look for the value that would apply for all tools
        try:
            return self.parameter_map[self.build_key(parameter_name, tool_name)]
        except KeyError:
            return self.parameter_map.get(parameter_name, None)

    def register_parameter(self, parameter_name, parameter_value, tool_name=None):
        self.parameter_map[self.build_key(parameter_name, tool_name)] = parameter_value

    def build_key(self, parameter_name, tool_name):
        if tool_name is None:
            return parameter_name
        return f"{parameter_name}{self.separator}{tool_name}"


def validate_path_exists(path):
    if not os.path.exists(path) or not os.path.isfile(os.path.realpath(path)):
        raise ApplicationException("The provided path (%s) does not exist or is not a valid file path." % path)


def validate_argument_is_directory(args, argument_name):
    file_name = getattr(args, argument_name)
    logging.info("REALPATH %s" % os.path.realpath(file_name))
    if file_name is not None and os.path.isdir(os.path.realpath(file_name)):
        raise ApplicationException("The provided output file name (%s) points to a directory." % file_name)


def validate_argument_is_valid_path(args, argument_name):
    paths_to_check = []
    # check if we are handling a single file or a list of files
    member_value = getattr(args, argument_name)
    if member_value is not None:
        if isinstance(member_value, list):
            for file_name in member_value:
                paths_to_check.append(str(file_name).strip())
        else:
            paths_to_check.append(str(member_value).strip())

        for path_to_check in paths_to_check:
            try:
                validate_path_exists(path_to_check)
            except ApplicationException:
                raise ApplicationException(f"Argument {argument_name}: The provided output file name ({path_to_check}) points to a directory.")


# taken from
# http://stackoverflow.com/questions/8384737/python-extract-file-name-from-path-no-matter-what-the-os-path-format
def get_filename(path):
    head, tail = ntpath.split(path)
    return tail or ntpath.basename(head)


def get_filename_without_suffix(path):
    root, ext = os.path.splitext(os.path.basename(path))
    return root


def parse_input_ctds(xsd_location, input_ctds, output_destination, output_file_extension):
    is_converting_multiple_ctds = len(input_ctds) > 1
    parsed_ctds = []
    schema = None
    if xsd_location is not None:
        try:
            logging.info("Loading validation schema from %s" % xsd_location)
            schema = etree.XMLSchema(etree.parse(xsd_location))
        except Exception as e:
            logging.error("Could not load validation schema {}. Reason: {}".format(xsd_location, str(e)))
    else:
        logging.warning("Validation against a schema has not been enabled.")

    for input_ctd in input_ctds:
        if schema is not None:
            validate_against_schema(input_ctd, schema)

        output_file = output_destination
        # if multiple inputs are being converted, we need to generate a different output_file for each input
        if is_converting_multiple_ctds:
            output_file = os.path.join(output_file, get_filename_without_suffix(input_ctd) + "." + output_file_extension)
        logging.info("Parsing %s" % input_ctd)

        model = None
        try:
            model = CTDModel(from_file=input_ctd)
        except ModelTypeError:
            pass
        try:
            model = Parameters(from_file=input_ctd)
        except ModelTypeError:
            pass
        assert model is not None, "Could not parse %s, seems to be no CTD/PARAMS" % (input_ctd)

        parsed_ctds.append(ParsedCTD(model, input_ctd, output_file))

    return parsed_ctds


def flatten_list_of_lists(args, list_name):
    lst = getattr(args, list_name)
    ret = []
    for e in lst:
        if isinstance(e, list):
            ret.extend(e)
        else:
            ret.append(e)
    setattr(args, list_name, ret)


def validate_against_schema(ctd_file, schema):
    try:
        parser = etree.XMLParser(schema=schema)
        etree.parse(ctd_file, parser=parser)
    except etree.XMLSyntaxError as e:
        raise ApplicationException("Invalid CTD file {}. Reason: {}".format(ctd_file, str(e)))


def add_common_parameters(parser, version, last_updated):
    parser.add_argument("FORMAT", default=None, help="Output format (mandatory). Can be one of: cwl, galaxy.")
    parser.add_argument("-i", "--input", dest="input_files", required=True, nargs="+", action="append",
                        help="List of CTD files to convert.")
    parser.add_argument("-o", "--output-destination", dest="output_destination", required=True,
                        help="If multiple input files are given, then a folder in which all converted "
                             "files will be generated is expected; "
                             "if a single input file is given, then a destination file is expected.")
    parser.add_argument("-x", "--default-executable-path", dest="default_executable_path",
                        help="Use this executable path when <executablePath> is not present in the CTD",
                        default=None, required=False)
    parser.add_argument("-p", "--hardcoded-parameters", dest="hardcoded_parameters", default=None, required=False,
                        help="File containing hardcoded values for the given parameters. Run with '-h' or '--help' "
                             "to see a brief example on the format of this file.")
    parser.add_argument("-V", "--validation-schema", dest="xsd_location", default=None, required=False,
                        help="Location of the schema to use to validate CTDs. If not provided, no schema validation "
                             "will take place.")

    # TODO: add verbosity, maybe?
    program_version = "v%s" % version
    program_build_date = str(last_updated)
    program_version_message = f"%(prog)s {program_version} ({program_build_date})"
    parser.add_argument("-v", "--version", action="version", version=program_version_message)


def parse_hardcoded_parameters(hardcoded_parameters_file):
    parameter_hardcoder = ParameterHardcoder()
    if hardcoded_parameters_file is None:
        return parameter_hardcoder
    with open(hardcoded_parameters_file) as fp:
        data = json.load(fp)

    for parameter_name in data:
        if parameter_name == "#":
            continue
        for el in data[parameter_name]:
            hardcoded_value = el.get("value", None)
            tool_names = el.get("tools", [None])
            for tool_name in tool_names:
                if tool_name is not None:
                    tool_name = tool_name.strip()

                # hardcoded / blacklisted:
                # - blacklisted: if value is @
                # - hardcoded: otherwise
                if hardcoded_value is not None:
                    if hardcoded_value == '@':
                        parameter_hardcoder.register_blacklist(parameter_name, tool_name)
                    else:
                        parameter_hardcoder.register_parameter(parameter_name, hardcoded_value, tool_name)
                else:
                    for a in el:
                        if a in ["tools", "value"]:
                            continue
                        if el[a] == "output-file":
                            el[a] = _OutFile
                        if el[a] == "input-file":
                            el[a] = _InFile

                        parameter_hardcoder.register_attribute(parameter_name, a, el[a], tool_name)

    return parameter_hardcoder


def extract_tool_help_text(ctd_model):
    manual = ""
    doc_url = None
    if "manual" in ctd_model.opt_attribs.keys():
        manual += "%s\n\n" % ctd_model.opt_attribs["manual"]
    if "docurl" in ctd_model.opt_attribs.keys():
        doc_url = ctd_model.opt_attribs["docurl"]

    help_text = "No help available"
    if manual is not None:
        help_text = manual
    if doc_url is not None:
        help_text = ("" if manual is None else manual)
        if doc_url != "":
            help_text += "\nFor more information, visit %s" % doc_url

    return help_text


def extract_tool_executable_path(model, default_executable_path):
    # rules to build the executable path:
    # if executablePath is null, then use default_executable_path
    # if executablePath is null and executableName is null, then the name of the tool will be used
    # if executablePath is null and executableName is not null, then executableName will be used
    # if executablePath is not null and executableName is null,
    #   then executablePath and the name of the tool will be used
    # if executablePath is not null and executableName is not null, then both will be used

    # first, check if the model has executablePath / executableName defined
    executable_path = model.opt_attribs.get("executablePath", None)
    executable_name = model.opt_attribs.get("executableName", None)

    # check if we need to use the default_executable_path
    if executable_path is None:
        executable_path = default_executable_path

    # fix the executablePath to make sure that there is a '/' in the end
    if executable_path is not None:
        executable_path = executable_path.strip()
        if not executable_path.endswith("/"):
            executable_path += "/"

    # assume that we have all information present
    command = str(executable_path) + str(executable_name)
    if executable_path is None:
        if executable_name is None:
            command = model.name
        else:
            command = executable_name
    else:
        if executable_name is None:
            command = executable_path + model.name
    return command


def _extract_and_flatten_parameters(parameter_group, nodes=False):
    """
    get the parameters of a OptionGroup as generator
    """
    for parameter in parameter_group.values():
        if type(parameter) is Parameter:
            yield parameter
        else:
            if nodes:
                yield parameter
            yield from _extract_and_flatten_parameters(parameter.parameters, nodes)


def extract_and_flatten_parameters(ctd_model, nodes=False):
    """
    get the parameters of a CTD as generator
    """
    if type(ctd_model) is CTDModel:
        return _extract_and_flatten_parameters(ctd_model.parameters.parameters, nodes)
    else:
        return _extract_and_flatten_parameters(ctd_model.parameters, nodes)

#     names = [_.name for _ in ctd_model.parameters.values()]
#     if names == ["version", "1"]:
#         return _extract_and_flatten_parameters(ctd_model.parameters.parameters["1"], nodes)
#     else:
#         return _extract_and_flatten_parameters(ctd_model, nodes)

#     for parameter in ctd_model.parameters.parameters:
#         if type(parameter) is not ParameterGroup:
#             yield parameter
#         else:
#             for p in extract_and_flatten_parameters(parameter):
#                 yield p

#     parameters = []
#     if len(ctd_model.parameters.parameters) > 0:
#         # use this to put parameters that are to be processed
#         # we know that CTDModel has one parent ParameterGroup
#         pending = [ctd_model.parameters]
#         while len(pending) > 0:
#             # take one element from 'pending'
#             parameter = pending.pop()
#             if type(parameter) is not ParameterGroup:
#                 parameters.append(parameter)
#             else:
#                 # append the first-level children of this ParameterGroup
#                 pending.extend(parameter.parameters.values())
#     # returned the reversed list of parameters (as it is now,
#     # we have the last parameter in the CTD as first in the list)
#     return reversed(parameters)


def extract_param_path(param, fix_dedup_prefix=False):
    pl = param.get_lineage(name_only=True)
    if fix_dedup_prefix:
        for i, p in enumerate(pl):
            dedup_match = re.match("DEDUP_[0-9]+_DEDUP_(.*)", p)
            if dedup_match:
                pl[i] = dedup_match.group(1)
    return pl


def extract_param_name(param, fix_dedup_prefix=False):
    # we generate parameters with colons for subgroups, but not for the two topmost parents (OpenMS legacy)
    return ":".join(extract_param_path(param, fix_dedup_prefix))


def extract_command_line_prefix(param, ctd_model):
    return "-" + extract_param_name(param, True)


def indent(s, indentation="  "):
    """
    helper function to indent text
    @param s the text (a string)
    @param indentation the desired indentation
    @return indented text
    """
    return [indentation + _ for _ in s]


def getFromDict(dataDict, mapList):
    return reduce(operator.getitem, mapList, dataDict)


def setInDict(dataDict, mapList, value):
    getFromDict(dataDict, mapList[:-1])[mapList[-1]] = value
