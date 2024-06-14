#!/usr/bin/env python
import copy
import json
import logging
import os
import os.path
import random
import re
import sys
from collections import OrderedDict

from CTDopts.CTDopts import (
    _Choices,
    _FileFormat,
    _InFile,
    _Null,
    _NumericRange,
    _OutFile,
    _OutPrefix,
    ModelError,
    ParameterGroup
)
from lxml import etree
from lxml.etree import (
    CDATA,
    Element,
    ElementTree,
    parse,
    ParseError,
    strip_elements,
    SubElement
)

from ..common import (
    utils
)
from ..common.exceptions import (
    ApplicationException,
    InvalidModelException
)

# mapping to CTD types to Galaxy types
TYPE_TO_GALAXY_TYPE = {int: 'integer', float: 'float', str: 'text', bool: 'boolean', _InFile: 'txt',
                       _OutFile: 'txt', _Choices: 'select', _OutPrefix: 'output-prefix'}
GALAXY_TYPE_TO_TYPE = dict()
for k in TYPE_TO_GALAXY_TYPE:
    GALAXY_TYPE_TO_TYPE[TYPE_TO_GALAXY_TYPE[k]] = k

STDIO_MACRO_NAME = "stdio"
REQUIREMENTS_MACRO_NAME = "requirements"
ADVANCED_OPTIONS_NAME = "adv_opts"

REQUIRED_MACROS = [REQUIREMENTS_MACRO_NAME, STDIO_MACRO_NAME, ADVANCED_OPTIONS_NAME + "_macro"]

random.seed(42)


class ExitCode:
    def __init__(self, code_range="", level="", description=None):
        self.range = code_range
        self.level = level
        self.description = description


class DataType:
    def __init__(self, extension, galaxy_extension, composite=None):
        self.extension = extension
        self.galaxy_extension = galaxy_extension
        self.composite = composite


def add_specific_args(parser):
    """
    add command line arguments specific for galaxy tool generation
    @param parser an instance of ArgumentParser
    """
    parser.add_argument("-f", "--formats-file", dest="formats_file",
                        help="File containing the supported file formats. Run with '-h' or '--help' to see a "
                             "brief example on the layout of this file.", default=None, required=False)
    parser.add_argument("-a", "--add-to-command-line", dest="add_to_command_line",
                        help="Adds content to the command line", default="", required=False)
    parser.add_argument("-r", "--required-tools", dest="required_tools_file", default=None, required=False,
                        help="Each line of the file will be interpreted as a tool name that needs translation. "
                        "Run with '-h' or '--help' to see a brief example on the format of this file.")
    parser.add_argument("-s", "--skip-tools", dest="skip_tools_file", default=None, required=False,
                        help="File containing a list of tools for which a Galaxy stub will not be generated. "
                        "Run with '-h' or '--help' to see a brief example on the format of this file.")
    parser.add_argument("-m", "--macros", dest="macros_files", default=[], nargs="*",
                        action="append", required=None, help="Import the additional given file(s) as macros. "
                        "The macros stdio, requirements and advanced_options are "
                        "required. Please see galaxy/macros.xml for an example of a "
                        "valid macros file. All defined macros will be imported.")
    parser.add_argument("--test-macros", dest="test_macros_files", default=[], nargs="*",
                        action="append", required=None,
                        help="Insert tests from the files given file(s) in the test section. "
                        "The macro names must end with the id of the tools")
    parser.add_argument("--test-macros-prefix", dest="test_macros_prefix", default=[], nargs="*",
                        action="append", required=None, help="The prefix of the macro name in the corresponding test macros file")
    parser.add_argument("--test-test", dest="test_test", action='store_true', default=False, required=False,
                        help="Generate a simple test for the internal unit tests.")

    parser.add_argument("--test-only", dest="test_only", action='store_true', default=False, required=False,
                        help="Generate only the test section.")
    parser.add_argument("--test-unsniffable", dest="test_unsniffable", nargs="+", default=[], required=False,
                        help="File extensions that can't be sniffed in Galaxy."
                        "Needs to be the OpenMS extensions (1st column in --formats-file)."
                        "For testdata with such extensions ftype will be set in the tes according to the file extension")
    parser.add_argument("--test-condition", dest="test_condition", nargs="+", default=["compare=sim_size", "delta_frac=0.05"], required=False,
                        help="Condition for dile tests")

    parser.add_argument("--tool-version", dest="tool_version", required=False, default=None,
                        help="Tool version to use (if not given its extracted from the CTD)")
    parser.add_argument("--tool-profile", dest="tool_profile", required=False, default=None,
                        help="Tool profile version to use (if not given its not set)")
    parser.add_argument("--bump-file", dest="bump_file", required=False,
                        default=None, help="json file defining tool versions."
                        "tools not listed in the file default to 0."
                        "if not given @VERSION_SUFFIX@ is used")


def modify_param_for_galaxy(param):
    """
    some parameters need galaxy specific modifications
    """
    if param.type is _InFile:
        # if file default is given (happens for external applications and
        # files for which the default is taken from share/OpenMS) set the
        # parm to not required and remove the default (external applications
        # need to be taken care by hardcoded values and the other cases
        # are chosen automatically if not specified on the command line)
        if param.required and not (param.default is None or type(param.default) is _Null):
            logging.warning(f"Data parameter {param.name} with default ({param.default})")
            param.required = False
            param.default = _Null()
    return param


def convert_models(args, parsed_ctds):
    """
    main conversion function

    @param args command line arguments
    @param parsed_ctds the ctds
    """

    # validate and prepare the passed arguments
    validate_and_prepare_args(args, parsed_ctds[0].ctd_model)

    # parse the given supported file-formats file
    supported_file_formats = parse_file_formats(args.formats_file)

    # extract the names of the macros and check that we have found the ones we need
    macros_to_expand = parse_macros_files(args.macros_files,
                                          tool_version=args.tool_version,
                                          supported_file_types=supported_file_formats,
                                          required_macros=REQUIRED_MACROS,
                                          dont_expand=[ADVANCED_OPTIONS_NAME + "_macro", "references",
                                                       "list_string_val", "list_string_san",
                                                       "list_float_valsan", "list_integer_valsan"])

    bump = parse_bump_file(args.bump_file)

    check_test_macros(args.test_macros_files, args.test_macros_prefix, parsed_ctds)

    # parse the skip/required tools files
    skip_tools = parse_tools_list_file(args.skip_tools_file)
    required_tools = parse_tools_list_file(args.required_tools_file)
    _convert_internal(parsed_ctds,
                      supported_file_formats=supported_file_formats,
                      default_executable_path=args.default_executable_path,
                      add_to_command_line=args.add_to_command_line,
                      required_tools=required_tools,
                      skip_tools=skip_tools,
                      macros_file_names=args.macros_files,
                      macros_to_expand=macros_to_expand,
                      parameter_hardcoder=args.parameter_hardcoder,
                      test_test=args.test_test,
                      test_only=args.test_only,
                      test_condition=args.test_condition,
                      test_unsniffable=args.test_unsniffable,
                      test_macros_file_names=args.test_macros_files,
                      test_macros_prefix=args.test_macros_prefix,
                      tool_version=args.tool_version,
                      tool_profile=args.tool_profile,
                      bump=bump)


def parse_bump_file(bump_file):
    if bump_file is None:
        return None
    with open(bump_file) as fp:
        return json.load(fp)


def parse_tools_list_file(tools_list_file):
    """
    """
    tools_list = None
    if tools_list_file is not None:
        tools_list = []
        with open(tools_list_file) as f:
            for line in f:
                if line is None or not line.strip() or line.strip().startswith("#"):
                    continue
                else:
                    tools_list.append(line.strip())

    return tools_list


def parse_macros_files(macros_file_names, tool_version, supported_file_types, required_macros=[], dont_expand=[]):
    """
    """
    macros_to_expand = []
    for macros_file_name in macros_file_names:
        try:
            macros_file = open(macros_file_name)
            logging.info("Loading macros from %s" % macros_file_name)
            root = parse(macros_file).getroot()
            for xml_element in root.findall("xml"):
                name = xml_element.attrib["name"]
                if name in macros_to_expand:
                    logging.warning("Macro %s has already been found. Duplicate found in file %s." % (name, macros_file_name))
                    continue
                logging.info("Macro %s found" % name)
                macros_to_expand.append(name)
        except ParseError as e:
            raise ApplicationException("The macros file " + macros_file_name + " could not be parsed. Cause: " + str(e))

        except OSError as e:
            raise ApplicationException("The macros file " + macros_file_name + " could not be opened. Cause: " + str(e))
        else:
            macros_file.close()

    tool_ver_tk = root.find("token[@name='@TOOL_VERSION@']")
    galaxy_ver_tk = root.find("token[@name='@VERSION_SUFFIX@']")
    if tool_ver_tk is None:
        tool_ver_tk = add_child_node(root, "token", OrderedDict([("name", "@TOOL_VERSION@")]))
        tool_ver_tk.text = tool_version
    if galaxy_ver_tk is not None:
        if tool_version == tool_ver_tk.text:
            galaxy_ver_tk.text = str(int(galaxy_ver_tk.text))
        else:
            tool_ver_tk.text = tool_version
            galaxy_ver_tk.text = "0"

    ext_foo = root.find("token[@name='@EXT_FOO@']")
    if ext_foo is None:
        ext_foo = add_child_node(root, "token", OrderedDict([("name", "@EXT_FOO@")]))

    g2o, o2g = get_fileformat_maps(supported_file_types)

    # make sure that the backup data type is in the map
    if 'txt' not in g2o:
        g2o['txt'] = 'txt'

    ext_foo.text = CDATA("""#def oms2gxyext(o)
    #set m={}
    #return m[o]
#end def
#def gxy2omsext(g)
    #set m={}
    #return m[g]
#end def
""".format(str(o2g), str(g2o)))

    tree = ElementTree(root)
    tree.write(macros_file_name, encoding="UTF-8", xml_declaration=True, pretty_print=True)
#     with open(macros_file_name, "w") as macros_file:
#         tree = ElementTree(root)
#         tree.write(macros_file, encoding="UTF-8", xml_declaration=True, pretty_print=True)

    # we depend on "stdio", "requirements" and "advanced_options" to exist on all the given macros files
    missing_needed_macros = []
    for required_macro in required_macros:
        if required_macro not in macros_to_expand:
            missing_needed_macros.append(required_macro)

    if missing_needed_macros:
        raise ApplicationException(
            "The following required macro(s) were not found in any of the given macros files: %s, "
            "see galaxy/macros.xml for an example of a valid macros file."
            % ", ".join(missing_needed_macros))

    # remove macros that should not be expanded
    for m in dont_expand:
        try:
            idx = macros_to_expand.index(m)
            del macros_to_expand[idx]
        except ValueError:
            pass

    return macros_to_expand


def check_test_macros(test_macros_files, test_macros_prefix, parsed_ctds):

    tool_ids = set()
    for parsed_ctd in parsed_ctds:
        model = parsed_ctd.ctd_model
        tool_ids.add(model.name.replace(" ", "_"))

    for mf, mp in zip(test_macros_files, test_macros_prefix):
        macro_ids = set()
        try:
            with open(mf) as macros_file:
                root = parse(macros_file).getroot()
                for xml_element in root.findall("xml"):
                    name = xml_element.attrib["name"]
                    if not name.startswith(mp):
                        logging.warning("Testmacro with invalid prefix %s." % (mp))
                        continue
                    name = name[len(mp):]
                    macro_ids.add(name)

        except ParseError as e:
            raise ApplicationException("The macros file " + mf + " could not be parsed. Cause: " + str(e))
        except OSError as e:
            raise ApplicationException("The macros file " + mf + " could not be opened. Cause: " + str(e))
        for t in tool_ids - macro_ids:
            logging.error("missing %s" % t)
            add_child_node(root, "xml", OrderedDict([("name", mp + t)]))

        if len(macro_ids - tool_ids):
            logging.warning("Unnecessary macros in {}: {}".format(mf, macro_ids - tool_ids))
        tree = ElementTree(root)
        tree.write(mf, encoding="UTF-8", xml_declaration=True, pretty_print=True)


def parse_file_formats(formats_file):
    """
    """
    supported_formats = []
    if formats_file is not None:
        line_number = 0
        with open(formats_file) as f:
            for line in f:
                line_number += 1
                if line is None or not line.strip() or line.strip().startswith("#"):
                    # ignore (it'd be weird to have something like:
                    # if line is not None and not (not line.strip()) ...
                    continue
                parsed_formats = line.strip().split()
                # valid lines contain either one or two columns
                if len(parsed_formats) == 1:
                    supported_formats.append(DataType(parsed_formats[0], parsed_formats[0]))
                elif len(parsed_formats) == 2:
                    supported_formats.append(DataType(parsed_formats[0], parsed_formats[1]))
                elif len(parsed_formats) == 3:
                    composite = [tuple(x.split(":")) for x in parsed_formats[2].split(",")]

                    supported_formats.append(DataType(parsed_formats[0],
                                                      parsed_formats[1],
                                                      composite))
                else:
                    logging.warning("Invalid line at line number %d of the given formats file. Line will be ignored:\n%s" % (line_number, line))
    return supported_formats


def get_fileformat_maps(supported_formats):
    """
    convenience functions to compute dictionaries mapping
    Galaxy data types <-> CTD formats
    """
    o2g = {}
    g2o = {}
    for s in supported_formats:
        if s.extension not in o2g:
            o2g[s.extension] = s.galaxy_extension
        if s.galaxy_extension not in g2o:
            g2o[s.galaxy_extension] = s.extension
    return g2o, o2g


def validate_and_prepare_args(args, model):
    """
    check command line arguments
    @param args command line arguments
    @return None
    """
    # check that only one of skip_tools_file and required_tools_file has been provided
    if args.skip_tools_file is not None and args.required_tools_file is not None:
        raise ApplicationException(
            "You have provided both a file with tools to ignore and a file with required tools.\n"
            "Only one of -s/--skip-tools, -r/--required-tools can be provided.")

    # flatten macros_files to make sure that we have a list containing file names and not a list of lists
    utils.flatten_list_of_lists(args, "macros_files")
    utils.flatten_list_of_lists(args, "test_macros_files")
    utils.flatten_list_of_lists(args, "test_macros_prefix")

    # check that the arguments point to a valid, existing path
    input_variables_to_check = ["skip_tools_file", "required_tools_file", "macros_files", "formats_file"]
    for variable_name in input_variables_to_check:
        utils.validate_argument_is_valid_path(args, variable_name)

    if not args.macros_files:
        # list is empty, provide the default value
        logging.warning("Using default macros from galaxy/macros.xml")
        args.macros_files = [os.path.dirname(os.path.abspath(__file__)) + "/macros.xml"]

    if args.tool_version is None:
        args.tool_version = model.version


def get_preferred_file_extension():
    """
    get the file extension for the output files
    @return "xml"
    """
    return "xml"


def _convert_internal(parsed_ctds, **kwargs):
    """
    parse all input files into models using CTDopts (via utils)

    @param parsed_ctds the ctds
    @param kwargs skip_tools, required_tools, and additional parameters for
        expand_macros, create_command, create_inputs, create_outputs
    @return a tuple containing the model, output destination, origin file
    """
    parameter_hardcoder = kwargs["parameter_hardcoder"]
    for parsed_ctd in parsed_ctds:
        model = parsed_ctd.ctd_model

        if kwargs["skip_tools"] is not None and model.name in kwargs["skip_tools"]:
            logging.info("Skipping tool %s" % model.name)
            continue
        elif kwargs["required_tools"] is not None and model.name not in kwargs["required_tools"]:
            logging.info("Tool %s is not required, skipping it" % model.name)
            continue

        origin_file = parsed_ctd.input_file
        output_file = parsed_ctd.suggested_output_file

        # overwrite attributes of the parsed ctd parameters as specified in hardcoded parameters json
        for param in utils.extract_and_flatten_parameters(model):
            hardcoded_attributes = parameter_hardcoder.get_hardcoded_attributes(utils.extract_param_name(param), model.name, 'CTD')
            if hardcoded_attributes is not None:
                for a in hardcoded_attributes:
                    if not hasattr(param, a):
                        continue
                    if a == "type":
                        try:
                            t = GALAXY_TYPE_TO_TYPE[hardcoded_attributes[a]]
                        except KeyError:
                            logging.error("Could not set hardcoded attribute {}={} for {}".format(a, hardcoded_attributes[a], param.name))
                            sys.exit(1)
                        setattr(param, a, t)
                    elif a == "restrictions":
                        if type(getattr(param, a)) is _FileFormat or param.type in [_InFile, _OutFile, _OutPrefix]:
                            setattr(param, a, _FileFormat(str(hardcoded_attributes[a])))
                        elif type(getattr(param, a)) is _Choices:
                            setattr(param, a, _Choices(str(hardcoded_attributes[a])))
                        elif type(getattr(param, a)) is _NumericRange:
                            raise Exception("Overwriting of Numeric Range not implemented")
                        elif getattr(param, a) is None:
                            # if the parameter has no restrictions yet the we just use Choices .. might need to be extended
                            setattr(param, a, _Choices(str(hardcoded_attributes[a])))
                        else:
                            setattr(param, a, hardcoded_attributes[a])
                    else:
                        setattr(param, a, hardcoded_attributes[a])

        if "test_only" in kwargs and kwargs["test_only"]:
            test = create_test_only(parsed_ctd.ctd_model, **kwargs)
            tree = ElementTree(test)
            output_file = parsed_ctd.suggested_output_file
            logging.info("Writing to %s" % utils.get_filename(output_file))
            tree.write(output_file, encoding="UTF-8", xml_declaration=False, pretty_print=True)
            continue

        logging.info("Converting {} (source {})".format(model.name, utils.get_filename(origin_file)))
        tool = create_tool(model,
                           kwargs.get("tool_profile", None),
                           kwargs.get("bump", None))
        write_header(tool, model)
        create_description(tool, model)
        import_macros(tool, model, **kwargs)
        expand_macros(tool, kwargs["macros_to_expand"])
#         command, inputs, outputs = create_cio(tool, model, **kwargs)
        create_command(tool, model, **kwargs)
        create_configfiles(tool, model, **kwargs)
        inputs = create_inputs(tool, model, **kwargs)
        outputs = create_outputs(tool, model, **kwargs)
        if kwargs["test_test"]:
            create_tests(tool, inputs=copy.deepcopy(inputs), outputs=copy.deepcopy(outputs))
        else:
            add_child_node(tool, "tests")

        create_help(tool, model)
        # citations are required to be at the end
        expand_macro(tool, "references")
        add_macros(tool, model, kwargs.get("test_macros_prefix"), kwargs.get("test_macros_file_names"))

        # wrap our tool element into a tree to be able to serialize it
        tree = ElementTree(tool)
        logging.info("Writing to %s" % utils.get_filename(output_file))
        etree.indent(tree)
        tree.write(output_file, encoding="UTF-8", pretty_print=True)

def write_header(tool, model):
    """
    add comments to the tool header
    @param tool the tool xml
    @param model the ctd model
    """
    tool.addprevious(etree.Comment(
        "This is a configuration file for the integration of a tools into Galaxy (https://galaxyproject.org/). "
        "This file was automatically generated using CTDConverter."))
    tool.addprevious(etree.Comment('Proposed Tool Section: [%s]' % model.opt_attribs.get("category", "")))


def create_tool(model, profile, bump):
    """
    initialize the tool
    @param model the ctd model
    """

    tool_id = model.name.replace(" ", "_")

    if bump is None:
        gxy_version = "@VERSION_SUFFIX@"
    elif model.name in bump:
        gxy_version = str(bump[model.name])
    elif tool_id in bump:
        gxy_version = str(bump[tool_id])
    else:
        gxy_version = "@VERSION_SUFFIX@"

    attrib = OrderedDict([("id", tool_id),
                          ("name", model.name),
                          ("version", "@TOOL_VERSION@+galaxy" + gxy_version)])
    if profile is not None:
        attrib["profile"] = profile
    return Element("tool", attrib)


def create_description(tool, model):
    """
    add description to the tool
    @param tool the Galaxy tool
    @param model the ctd model
    """
    if "description" in model.opt_attribs.keys() and model.opt_attribs["description"] is not None:
        description = SubElement(tool, "description")
        text = model.opt_attribs["description"]
        text = text.split("\n")[0].strip()
        if text[-1] == ".":
            text = text[:-1]
        description.text = text


def create_configfiles(tool, model, **kwargs):
    """
    create
    - <configfiles><inputs>
    - <configfiles><configfile>

    The former will create a json file containing the tool parameter values
    that can be accessed in cheetah with $args_json. Note that
    data_style="paths" (i.e. input data sets are included in the json) is set
    even if input files are given on the CLI. Reason is that in this way
    default values in the CTD can be restored for optional input files.

    The latter will contain hardcoded parameters.
    """

    configfiles_node = add_child_node(tool, "configfiles")
    add_child_node(configfiles_node, "inputs",
                   OrderedDict([("name", "args_json"), ("data_style", "paths")]))

    parameter_hardcoder = kwargs.get("parameter_hardcoder")
    hc_dict = dict()
    for param in utils.extract_and_flatten_parameters(model):
        hardcoded_value = parameter_hardcoder.get_hardcoded_value(utils.extract_param_name(param), model.name)
        if hardcoded_value is None:
            continue
        path = utils.extract_param_path(param)
        for i, v in enumerate(path[:-1]):
            try:
                utils.getFromDict(hc_dict, path[:i + 1])
            except KeyError:
                utils.setInDict(hc_dict, path[:i + 1], {})
        utils.setInDict(hc_dict, path, hardcoded_value)
    hc_node = add_child_node(configfiles_node, "configfile",
                             OrderedDict([("name", "hardcoded_json")]))
    hc_node.text = CDATA(json.dumps(hc_dict).replace('$', r'\$'))
    # print(json.dumps(hc_dict))


def create_command(tool, model, **kwargs):
    """
    @param tool the Galaxy tool
    @param model the ctd model
    @param kwargs
    """

    # main command
    final_cmd = OrderedDict([('preprocessing', []), ('command', []), ('postprocessing', [])])
    advanced_cmd = {'preprocessing': [], 'command': [], 'postprocessing': []}

    final_cmd['preprocessing'].extend(["@QUOTE_FOO@", "@EXT_FOO@", "#import re", "", "## Preprocessing"])

    # - call the executable with -write_ctd to write the ctd file (with defaults)
    # - use fill_ctd.py to overwrite the defaults in the ctd file with the
    #   Galaxy parameters in the JSON file (from inputs config file)
    # - feed the ctd file to the executable (with -ini)
    #   note: input and output file parameters are still given on the command line
    #   - output file parameters are not included in the JSON file
    #   - input and output files are accessed through links / files that have the correct extension
    final_cmd['command'].extend(["", "## Main program call"])
    final_cmd['command'].append("""
set -o pipefail &&
@EXECUTABLE@ -write_ctd ./ &&
python3 '$__tool_directory__/fill_ctd.py' '@EXECUTABLE@.ctd' '$args_json' '$hardcoded_json' &&
@EXECUTABLE@ -ini @EXECUTABLE@.ctd""")
    final_cmd['command'].extend(kwargs["add_to_command_line"])
    final_cmd['postprocessing'].extend(["", "## Postprocessing"])

    advanced_command_start = "## advanced options"
    advanced_command_end = ""

    parameter_hardcoder = kwargs["parameter_hardcoder"]
    supported_file_formats = kwargs["supported_file_formats"]
    g2o, o2g = get_fileformat_maps(supported_file_formats)

    for param in utils.extract_and_flatten_parameters(model):
        param = modify_param_for_galaxy(param)

        param_cmd = {'preprocessing': [], 'command': [], 'postprocessing': []}
        command_line_prefix = utils.extract_command_line_prefix(param, model)

        # TODO use utils.extract_param_name(param).replace(":", "_")? Then hardcoding ctd variables (with :) and tool variables (with _) can be distinguished
        if parameter_hardcoder.get_blacklist(utils.extract_param_name(param), model.name):
            continue
        hardcoded_value = parameter_hardcoder.get_hardcoded_value(utils.extract_param_name(param), model.name)
        if hardcoded_value is not None:
            pass  # TODO hardcoded values should go to <inputs>
            # param_cmd['command'].append("%s %s" % (command_line_prefix, hardcoded_value))
        else:
            # in the else branch the parameter is neither blacklisted nor hardcoded...

            _actual_parameter = get_galaxy_parameter_path(param)
            actual_parameter = get_galaxy_parameter_path(param, fix_underscore=True)
            # all but bool params need the command line argument (bools have it already in the true/false value)
            if param.type is _OutFile or param.type is _OutPrefix or param.type is _InFile:
                param_cmd['command'].append(command_line_prefix)

            # preprocessing for file inputs:
            # - create a dir with name param.name
            # - create a link to id.ext in this directory
            # rationale: in the autogenerated tests the same file was used as input to multiple parameters
            # this leads to conflicts while linking... might also be better in general
            if param.type is _InFile:
                param_cmd['preprocessing'].append("mkdir %s &&" % actual_parameter)
                # TODO we copy input files to the JWD because of https://github.com/OpenMS/OpenMS/issues/7439
                if param.is_list:
                    param_cmd['preprocessing'].append(f'#if ${_actual_parameter}_select == "no"')
                    param_cmd['preprocessing'].append(f"mkdir ${{' '.join([\"'{actual_parameter}/%s'\" % (i) for i, f in enumerate(${_actual_parameter}) if f])}} && ")
                    param_cmd['preprocessing'].append(f"${{' '.join([\"cp '%s' '{actual_parameter}/%s/%s.%s' && \" % (f, i, re.sub('[^\\w\\-_]', '_', f.element_identifier), $gxy2omsext(f.ext)) for i, f in enumerate(${_actual_parameter}) if f])}}")
                    param_cmd['preprocessing'].append('#else')
                    param_cmd['preprocessing'].append(f"cp '${_actual_parameter}' '{actual_parameter}/${{re.sub(\"[^\\w\\-_]\", \"_\", ${_actual_parameter}.element_identifier)}}.$gxy2omsext(${_actual_parameter}.ext)' &&")
                    param_cmd['preprocessing'].append('#end if')
                    param_cmd['command'].append(f'#if ${actual_parameter}_select == "no"')
                    param_cmd['command'].append(f"${{' '.join([\"'{actual_parameter}/%s/%s.%s'\"%(i, re.sub('[^\\w\\-_]', '_', f.element_identifier), $gxy2omsext(f.ext)) for i, f in enumerate(${_actual_parameter}) if f])}}")
                    param_cmd['command'].append('#else')
                    param_cmd['command'].append(f"'{actual_parameter}/${{re.sub(\"[^\\w\\-_]\", \"_\", ${_actual_parameter}.element_identifier)}}.$gxy2omsext(${_actual_parameter}.ext)'")
                    param_cmd['command'].append('#end if')
                else:
                    param_cmd['preprocessing'].append("cp '$" + _actual_parameter + "' '" + actual_parameter + "/${re.sub(\"[^\\w\\-_]\", \"_\", $" + _actual_parameter + ".element_identifier)}.$gxy2omsext($" + _actual_parameter + ".ext)' &&")
                    param_cmd['command'].append("'" + actual_parameter + "/${re.sub(\"[^\\w\\-_]\", \"_\", $" + _actual_parameter + ".element_identifier)}.$gxy2omsext($" + _actual_parameter + ".ext)'")

            elif param.type is _OutPrefix:
                param_cmd['preprocessing'].append("mkdir %s &&" % actual_parameter)
                param_cmd['command'].append(actual_parameter + "/")
            elif param.type is _OutFile:
                _actual_parameter = get_galaxy_parameter_path(param, separator="_")
                actual_parameter = get_galaxy_parameter_path(param, separator="_", fix_underscore=True)
                # check if there is a parameter that sets the format
                # if so we add an extension to the generated files which will be used to
                # determine the format in the output tag
                # in all other cases (corresponding input / there is only one allowed format)
                # the format will be set in the output tag
                formats = get_galaxy_formats(param, model, o2g, TYPE_TO_GALAXY_TYPE[param.type])
                type_param = get_out_type_param(param, model, parameter_hardcoder)
                corresponding_input, fmt_from_corresponding = get_corresponding_input(param, model)
                # print("ci %s ffc %s" % (corresponding_input.name, fmt_from_corresponding))
                # print("formats %s" % (formats))
                if corresponding_input is not None:
                    actual_input_parameter = get_galaxy_parameter_path(corresponding_input)
                else:
                    actual_input_parameter = None
                # print(len(formats) > 1, (corresponding_input is None or not
                #                          fmt_from_corresponding))
                if type_param is not None:
                    type_param_name = get_galaxy_parameter_path(type_param)
                elif len(formats) > 1 and (corresponding_input is None or not
                                           fmt_from_corresponding):  # and not param.is_list:
                    type_param_name = get_galaxy_parameter_path(param, suffix="type")
                else:
                    type_param_name = None
                # print("tp %s" % type_param_name)

                param_cmd['preprocessing'].append("mkdir " + actual_parameter + " &&")

                # if there is only one format (the outoput node sets format using the format attribute of the data/discover node)
                # - single file: write to temp file with oms extension and move this to the actual result file
                # - lists: write to files with the oms extension and remove the extension afterwards (discovery with __name__)
                if len(formats) == 1:
                    fmt = formats.pop()
                    if param.is_list:
                        logging.info(f"1 fmt + list {param.name} -> {actual_input_parameter}")
                        param_cmd['preprocessing'].append("mkdir ${' '.join([\"'" + actual_parameter + "/%s'\" % (i) for i, f in enumerate($" + actual_input_parameter + ") if f])} && ")
                        param_cmd['command'].append("${' '.join([\"'" + actual_parameter + "/%s/%s.%s'\"%(i, re.sub('[^\\w\\-_]', '_', f.element_identifier), $gxy2omsext(\"" + fmt + "\")) for i, f in enumerate($" + actual_input_parameter + ") if f])}")
                        param_cmd['postprocessing'].append("${' '.join([\"&& mv -n '" + actual_parameter + "/%(bn)s/%(id)s.%(gext)s' '" + _actual_parameter + "/%(bn)s/%(id)s'\"%{\"bn\": i, \"id\": re.sub('[^\\w\\-_]', '_', f.element_identifier), \"gext\": $gxy2omsext(\"" + fmt + "\")} for i, f in enumerate($" + actual_input_parameter + ") if f])}")
                    else:
                        logging.info("1 fmt + dataset %s" % param.name)
                        param_cmd['command'].append("'" + actual_parameter + "/output.${gxy2omsext(\"" + fmt + "\")}'")
                        param_cmd['postprocessing'].append("&& mv '" + actual_parameter + "/output.${gxy2omsext(\"" + fmt + "\")}' '$" + _actual_parameter + "'")

                # if there is a type parameter then we use the type selected by the user
                # - single: write to temp file with the oms extension and mv it to the actual file output which is treated via change_format
                # - list: let the command create output files with the oms extensions, postprocessing renames them to the galaxy extensions, output is then discover + __name_and_ext__
                elif type_param_name is not None:
                    if param.is_list:
                        logging.info("type + list %s" % param.name)
                        param_cmd['preprocessing'].append("mkdir ${' '.join([\"'" + actual_parameter + "/%s'\" % (i) for i, f in enumerate($" + actual_input_parameter + ") if f])} && ")
                        param_cmd['command'].append("${' '.join([\"'" + actual_parameter + "/%s/%s.%s'\"%(i, re.sub('[^\\w\\-_]', '_', f.element_identifier), $" + type_param_name + ") for i, f in enumerate($" + actual_input_parameter + ") if f])}")
                        param_cmd['postprocessing'].append("${' '.join([\"&& mv -n '" + actual_parameter + "/%(bn)s/%(id)s.%(omsext)s' '" + actual_parameter + "/%(bn)s/%(id)s.%(gext)s'\"%{\"bn\": i, \"id\": re.sub('[^\\w\\-_]', '_', f.element_identifier), \"omsext\":$" + type_param_name + ", \"gext\": $oms2gxyext(str($" + type_param_name + "))} for i, f in enumerate($" + actual_input_parameter + ") if f])}")
                    else:
                        logging.info("type + dataset %s" % param.name)
                        # 1st create file with openms extension (often required by openms)
                        # then move it to the actual place specified by the parameter
                        # the format is then set by the <data> tag using <change_format>
                        param_cmd['command'].append("'" + actual_parameter + "/output.${" + type_param_name + "}'")
                        param_cmd['postprocessing'].append("&& mv '" + actual_parameter + "/output.${" + type_param_name + "}' '$" + actual_parameter + "'")
                elif actual_input_parameter is not None:
                    if param.is_list:
                        logging.info("actual + list %s" % param.name)
                        param_cmd['preprocessing'].append("mkdir ${' '.join([\"'" + actual_parameter + "/%s'\" % (i) for i, f in enumerate($" + actual_input_parameter + ") if f])} && ")
                        param_cmd['command'].append("${' '.join([\"'" + actual_parameter + "/%s/%s.%s'\"%(i, re.sub('[^\\w\\-_]', '_', f.element_identifier), f.ext) for i, f in enumerate($" + actual_input_parameter + ") if f])}")
                    else:
                        logging.info(f"actual + dataset {param.name} {actual_input_parameter} {corresponding_input.is_list}")
                        if corresponding_input.is_list:
                            param_cmd['command'].append("'" + actual_parameter + "/output.${" + actual_input_parameter + "[0].ext}'")
                            param_cmd['postprocessing'].append("&& mv '" + actual_parameter + "/output.${" + actual_input_parameter + "[0].ext}' '$" + _actual_parameter + "'")
                        else:
                            param_cmd['command'].append("'" + actual_parameter + "/output.${" + actual_input_parameter + ".ext}'")
                            param_cmd['postprocessing'].append("&& mv '" + actual_parameter + "/output.${" + actual_input_parameter + ".ext}' '$" + _actual_parameter + "'")
                else:
                    if param.is_list:
                        raise Exception("output parameter itemlist %s without corresponding input")
                    else:
                        logging.info("else + dataset %s" % param.name)
                        param_cmd['command'].append("'$" + _actual_parameter + "'")

#             # select with multiple = true
#             elif is_selection_parameter(param) and param.is_list:
#                 param_cmd['command'].append("${' '.join(['\"%s\"'%str(_) for _ in str($" + actual_parameter + ").split(',')])}")
#             elif param.is_list:
#                 param_cmd['command'].append("$quote($%s" % actual_parameter + ")")
#                 #command += "${' '.join([\"'%s'\"%str(_) for _ in $" + actual_parameter + "])}\n"
#             elif is_boolean_parameter(param):
#                 param_cmd['command'].append("$%s" % actual_parameter + "")
#             else:
#                 param_cmd['command'].append('"$' + actual_parameter + '"')

            # add if statement for optional parameters and preprocessing
            # - for optional outputs (param_out_x) the presence of the parameter
            #   depends on the additional input (param_x) -> need no if
            # - real string parameters (i.e. ctd type string wo restrictions) also
            #   need no if (otherwise the empty string could not be provided)
            if not (param.required or is_boolean_parameter(param) or (param.type is str and param.restrictions is None)):
                # and not(param.type is _InFile and param.is_list):
                actual_parameter = get_galaxy_parameter_path(param, suffix="FLAG", fix_underscore=True)
                _actual_parameter = get_galaxy_parameter_path(param, suffix="FLAG")
                for stage in param_cmd:
                    if len(param_cmd[stage]) == 0:
                        continue
                    # special case for optional itemlists: for those if no option is selected only the parameter must be specified
                    if is_selection_parameter(param) and param.is_list and param.required is False:
                        param_cmd[stage] = [param_cmd[stage][0]] + ["#if $" + _actual_parameter + ":"] + utils.indent(param_cmd[stage][1:]) + ["#end if"]
                    elif is_selection_parameter(param) or param.type is _InFile:
                        param_cmd[stage] = ["#if $" + _actual_parameter + ":"] + utils.indent(param_cmd[stage]) + ["#end if"]
                    elif param.type is _OutFile or param.type is _OutPrefix:
                        param_cmd[stage] = ["#if \"" + param.name + "_FLAG\" in str($OPTIONAL_OUTPUTS).split(',')"] + utils.indent(param_cmd[stage]) + ["#end if"]
                    else:
                        param_cmd[stage] = ["#if str($" + _actual_parameter + "):"] + utils.indent(param_cmd[stage]) + ["#end if"]

        for stage in param_cmd:
            if len(param_cmd[stage]) == 0:
                continue
            if param.advanced and hardcoded_value is None and not (param.type is _OutFile or param.type is _OutPrefix):
                advanced_cmd[stage].extend(param_cmd[stage])
            else:
                final_cmd[stage].extend(param_cmd[stage])

    for stage in advanced_cmd:
        if len(advanced_cmd[stage]) == 0:
            continue
        advanced_cmd[stage] = [advanced_command_start] + utils.indent(advanced_cmd[stage]) + [advanced_command_end]
        final_cmd[stage].extend(advanced_cmd[stage])

    out, optout = all_outputs(model, parameter_hardcoder)
    if len(optout) > 0 or len(out) + len(optout) == 0:
        stdout = ["| tee '$stdout'"]
        if len(optout) > 0:
            stdout = ["#if len(str($OPTIONAL_OUTPUTS).split(',')) == 0"] + utils.indent(stdout) + ["#end if"]
        final_cmd['command'].extend(stdout)

    ctd_out = ["#if \"ctd_out_FLAG\" in $OPTIONAL_OUTPUTS"] + utils.indent(["&& mv '@EXECUTABLE@.ctd' '$ctd_out'"]) + ["#end if"]
    final_cmd['postprocessing'].extend(ctd_out)
    command_node = add_child_node(tool, "command")
    command_node.attrib["detect_errors"] = "exit_code"

    command_node.text = CDATA("\n".join(sum(final_cmd.values(), [])))


def import_macros(tool, model, **kwargs):
    """
    creates the xml elements needed to import the needed macros files
    @param tool the Galaxy tool
    @param model the ctd model
    @param kwargs
    """
    macros_node = add_child_node(tool, "macros")
    token_node = add_child_node(macros_node, "token")
    token_node.attrib["name"] = "@EXECUTABLE@"
    token_node.text = utils.extract_tool_executable_path(model, kwargs["default_executable_path"])

    # add <import> nodes
    for macro_file_name in kwargs["macros_file_names"]:
        macro_file = open(macro_file_name)
        import_node = add_child_node(macros_node, "import")
        # do not add the path of the file, rather, just its basename
        import_node.text = os.path.basename(macro_file.name)


def add_macros(tool, model, test_macros_prefix=None, test_macros_file_names=None):
    """
    paste all test macros for a tool into the tests node of the tool
    """
    if test_macros_file_names is None:
        return
    tool_id = model.name.replace(" ", "_")
    tests_node = tool.find(".//tests")
    for macros_file, macros_prefix in zip(test_macros_file_names, test_macros_prefix):
        macros_root = parse(macros_file)
        tool_tests = macros_root.find(f".//xml[@name='{macros_prefix}{tool_id}']")
        for test in tool_tests:
            tests_node.append(test)

def expand_macro(node, macro, attribs=None):
    """Add <expand macro="..." ... /> to node."""
    expand_node = add_child_node(node, "expand")
    expand_node.attrib["macro"] = macro
    if attribs:
        for a in attribs:
            expand_node.attrib[a] = attribs[a]

    return expand_node


# and to "expand" the macros in a node
def expand_macros(node, macros_to_expand):
    # add <expand> nodes
    for expand_macro in macros_to_expand:
        expand_node = add_child_node(node, "expand")
        expand_node.attrib["macro"] = expand_macro


def get_galaxy_parameter_path(param, separator=".", suffix=None, fix_underscore=False):
    """
    Get the complete path for a parameter as a string where the path
    components are joined by the given separator. A given suffix can
    be appended
    """
    p = get_galaxy_parameter_name(param, suffix, fix_underscore)
    path = utils.extract_param_path(param, fix_underscore)
    if len(path) > 1:
        path = path[:-1] + [p]
    elif param.advanced and (param.type is not _OutFile and param.type is not _OutPrefix or suffix):
        path = [ADVANCED_OPTIONS_NAME, p]
    else:
        path = [p]
    # data input params with multiple="true" are in a (batch mode) conditional
    # which needs to be added to the param path list
    if param.type is _InFile and param.is_list:
        path.insert(len(path) - 1, f"{p}_cond")
    return separator.join(path).replace("-", "_")


def get_galaxy_parameter_name(param, suffix=None, fix_underscore=False):
    """
    get the name of the parameter used in the galaxy tool
    - replace : and - by _
    - add suffix for output parameters if not None

    the idea of suffix is to be used for optional outputs (out_x) for
    which an additional boolean input (out_x_FLAG) exists

    @param param the parameter
    @param suffix suffix to append
    @return the name used for the parameter in the tool form
    """
    p = param.name.replace("-", "_")
    if fix_underscore and p.startswith("_"):
        p = p[1:]
    if param.type is _OutFile and suffix is not None:
        return f"{p}_{suffix}"
    else:
        return "%s" % p


def get_out_type_param(out_param, model, parameter_hardcoder):
    """
    check if there is a parameter that has the same name with appended _type
    and return it if present, otherwise return None
    """
    if parameter_hardcoder.get_blacklist(out_param.name + "_type", model.name):
        return None

    for param in utils.extract_and_flatten_parameters(model):
        if param.name == out_param.name + "_type":
            return param
    return None


def is_in_type_param(param, model):
    return is_type_param(param, model, [_InFile])


def is_out_type_param(param, model):
    """
    check if the parameter is output_type parameter
    - the name ends with _type and there is an output parameter without this suffix
    and return True iff this is the case
    """
    return is_type_param(param, model, [_OutFile, _OutPrefix])


def is_type_param(param, model, tpe):
    """
    check if the parameter is _type parameter of an in/output
    - the name ends with _type and there is an output parameter without this suffix
    and return True iff this is the case
    """
    if not param.name.endswith("_type"):
        return False
    for out_param in utils.extract_and_flatten_parameters(model):
        if out_param.type not in tpe:
            continue
        if param.name == out_param.name + "_type":
            return True
    return False


def get_corresponding_input(out_param, model):
    """
    get the input parameter corresponding to the given output

    1st try to get the input with the type (single file/list) and same format restrictions
    if this fails get the input that has the same type
    in both cases there must be only one such input

    return the found input parameter and True iff the 1st case applied
    """
    c = get_input_with_same_restrictions(out_param, model, True)
    if c is None:
        return (get_input_with_same_restrictions(out_param, model, False), False)
    else:
        return (c, True)


def get_input_with_same_restrictions(out_param, model, check_formats):
    """
    get the input parameter that has the same restrictions (ctd file_formats)
    - input and output must both be lists of both be simple parameters
    """

    matching = []

    for allow_different_type in [False, True]:
        for param in utils.extract_and_flatten_parameters(model):
            if param.type is not _InFile:
                continue
#             logging.error("%s %s %s %s %s %s" %(out_param.name, param.name,  param.is_list, out_param.is_list, param.restrictions,  out_param.restrictions))
            if allow_different_type or param.is_list == out_param.is_list:
                if check_formats:
                    if param.restrictions is None and out_param.restrictions is None:
                        matching.append(param)
                    elif param.restrictions is not None and out_param.restrictions is not None and param.restrictions.formats == out_param.restrictions.formats:
                        matching.append(param)
                else:
                    matching.append(param)
#             logging.error("match %s "%([_.name for _ in matching]))
        if len(matching) > 0:
            break
    if len(matching) == 1:
        return matching[0]
    else:
        return None


def create_inputs(tool, model, **kwargs):
    """
    create input section of the Galaxy tool
    @param tool the Galaxy tool
    @param model the ctd model
    @param kwargs
    @return inputs node
    """
    inputs_node = SubElement(tool, "inputs")
    section_nodes = dict()
    section_params = dict()

    # some suites (such as OpenMS) need some advanced options when handling inputs
    advanced_node = Element("expand", OrderedDict([("macro", ADVANCED_OPTIONS_NAME + "_macro")]))
    parameter_hardcoder = kwargs["parameter_hardcoder"]
    supported_file_formats = kwargs["supported_file_formats"]
    g2o, o2g = get_fileformat_maps(supported_file_formats)

    # treat all non output-file/advanced/blacklisted/hardcoded parameters as inputs
    for param in utils.extract_and_flatten_parameters(model, True):
        if type(param) is ParameterGroup:
            title, help_text = generate_label_and_help(param.description)
            section_params[utils.extract_param_name(param)] = param
            section_nodes[utils.extract_param_name(param)] = Element("section", OrderedDict([("name", param.name), ("title", title), ("help", help_text), ("expanded", "false")]))
            continue

        param = modify_param_for_galaxy(param)
        # no need to show hardcoded parameters
        hardcoded_value = parameter_hardcoder.get_hardcoded_value(utils.extract_param_name(param), model.name)
        if hardcoded_value is not None:
            continue
        if parameter_hardcoder.get_blacklist(utils.extract_param_name(param), model.name):
            continue
        # do not output file type parameters for inputs since file types are
        # known by Galaxy and set automatically by extension (which comes from
        # the Galaxy data type which is translated to OpenMS datatype as defined
        # in filetypes.txt )
        if is_in_type_param(param, model):
            continue

        if utils.extract_param_name(param.parent) in section_nodes:
            parent_node = section_nodes[utils.extract_param_name(param.parent)]
        elif param.advanced:
            parent_node = advanced_node
        else:
            parent_node = inputs_node

        # sometimes special inputs are needed for outfiles:
        if param.type is _OutFile or param.type is _OutPrefix:
            # if there are multiple possible output formats, but no parameter to choose the type or a
            # corresponding input then add a selection parameter
            formats = get_galaxy_formats(param, model, o2g, TYPE_TO_GALAXY_TYPE[_OutFile])
            type_param = get_out_type_param(param, model, parameter_hardcoder)
            corresponding_input, fmt_from_corresponding = get_corresponding_input(param, model)
            if len(formats) > 1 and type_param is None and (corresponding_input is None or not
                                                            fmt_from_corresponding):  # and not param.is_list:
                fmt_select_attrib = OrderedDict([
                    ("name", param.name + "_type"),
                    ("type", "select"),
                    ("optional", "false"),
                    ("label", f"File type of output {param.name} ({param.description})")
                ])
                fmt_select = add_child_node(parent_node, "param", fmt_select_attrib)
                g2o, o2g = get_fileformat_maps(kwargs["supported_file_formats"])
                for choice in param.restrictions.formats:
                    option_node = add_child_node(fmt_select, "option", OrderedDict([("value", str(choice))]))
                    option_node.text = o2g[str(choice)]
                    if choice.lower() != o2g[str(choice)]:
                        option_node.text += f" ({choice})"
            continue

        # create the actual param node and fill the attributes
        param_node = create_param_attribute_list(param, model, kwargs["supported_file_formats"], parameter_hardcoder)
        parent_node.append(param_node)

    section_parents = [utils.extract_param_name(section_params[sn].parent) for sn in section_nodes]
    for sn in section_nodes:
        if len(section_nodes[sn]) == 0 and sn not in section_parents:
            continue
        if utils.extract_param_name(section_params[sn].parent) in section_nodes:
            section_nodes[utils.extract_param_name(section_params[sn].parent)].append(section_nodes[sn])
        else:
            inputs_node.append(section_nodes[sn])
    # if there is an advanced section then append it at the end of the inputs
    inputs_node.append(advanced_node)

    # Add select for optional outputs
    out, optout = all_outputs(model, parameter_hardcoder)
    attrib = OrderedDict([("name", "OPTIONAL_OUTPUTS"),
                          ("type", "select"),
                          ("optional", "true"),
                          ("multiple", "true"),
                          ("label", "Optional outputs")])
#     if len(out) == 0 and len(out) + len(optout) > 0:
#         attrib["optional"] = "false"
#     else:
#         attrib["optional"] = "true"
    param_node = add_child_node(inputs_node, "param", attrib)
    for o in optout:
        title, help_text = generate_label_and_help(o.description)
        option_node = add_child_node(param_node, "option",
                                     OrderedDict([("value", o.name + "_FLAG")]),
                                     text=f"{o.name} ({title})")
    option_node = add_child_node(param_node, "option",
                                 OrderedDict([("value", "ctd_out_FLAG")]),
                                 text="Output used ctd (ini) configuration file")

    return inputs_node


def is_default(value, param):
    """
    check if the value is the default of the param or if the value is in the defaults of param
    """
    return param.default == value or (type(param.default) is list and value in param.default)


def get_formats(param, model, o2g):
    """
    determine format attribute from the CTD restictions (i.e. the OpenMS extensions)
    - also check if all listed possible formats are supported in Galaxy and warn if necessary
    """

    if param.restrictions is None:
        return []
    elif type(param.restrictions) is _FileFormat:
        choices = param.restrictions.formats
    elif is_out_type_param(param, model):
        choices = param.restrictions.choices
    else:
        raise InvalidModelException(f"Unrecognized restriction type [{type(param.restrictions)}] for [{param.name}]")

    # check if there are formats that have not been registered yet...
    formats = set()
    for format_name in choices:
        if format_name not in o2g:
            logging.warning(f"Ignoring unknown format {format_name} for parameter {param.name}")
        else:
            formats.add(format_name)
    return sorted(formats)


def get_galaxy_formats(param, model, o2g, default=None):
    """
    determine galaxy formats for a parm (i.e. list of allowed Galaxy extensions)
    from the CTD restrictions (i.e. the OpenMS extensions)
    - if there is a single one, then take this
    - if there is none than use given default
    """
    formats = get_formats(param, model, o2g)
    gxy_formats = {o2g[_] for _ in formats if _ in o2g}
    if len(gxy_formats) == 0:
        if default is not None:
            gxy_formats.add(default)
        else:
            raise InvalidModelException("No supported formats [%(type)s] "
                                        "for [%(name)s]" % {"type": type(param.restrictions),
                                                            "name": param.name})
    return sorted(gxy_formats)


def get_oms_format(formats, gxy_format, o2g):
    """
    determine the format from a list of openms formats
    (restriction of a ctd input-file param) that corresponds
    to a galaxy format
    """
    for f in formats:
        if o2g.get(f) == gxy_format:
            return f


def create_param_attribute_list(param, model, supported_file_formats, parameter_hardcoder):
    """
    get the attributes of input parameters

    @param param the ctd parameter
    @param supported_file_formats
    """

    g2o, o2g = get_fileformat_maps(supported_file_formats)

    param_node = Element("param")
    # set the name, argument and a first guess for the type (which will be over written
    # in some cases .. see below)
    # even if the conversion relies on the fact that the param names are identical
    # to the ctd ITEM names we replace dashes by underscores because input and output
    # parameters need to be treated in cheetah. variable names are currently fixed back
    # to dashes in fill_ctd.py. currently there seems to be only a single tool
    # requiring this https://github.com/OpenMS/OpenMS/pull/4529
    argument = "-%s" % utils.extract_param_name(param)
    name = get_galaxy_parameter_name(param)
    if argument.lstrip("-").replace("-", "_") != name:
        param_node.attrib["name"] = get_galaxy_parameter_name(param)
    param_node.attrib["argument"] = argument

    param_type = TYPE_TO_GALAXY_TYPE[param.type]
    if param_type is None:
        raise ModelError("Unrecognized parameter type %(type)s for parameter %(name)s"
                         % {"type": param.type, "name": param.name})

    # ITEMLIST is rendered as text field (even if its integers or floats), an
    # exception is files which are treated just below
    if param.is_list:
        param_type = "text"
    if is_selection_parameter(param):
        param_type = "select"
    if is_boolean_parameter(param):
        param_type = "boolean"

    if param.type is _InFile:
        # assume it's just text unless restrictions are provided
        param_node.attrib["type"] = "data"
        param_node.attrib["format"] = ",".join(get_galaxy_formats(param, model, o2g, TYPE_TO_GALAXY_TYPE[_InFile]))
        # in the case of multiple input set multiple flag
        if param.is_list:
            param_node.attrib["multiple"] = "true"
    else:
        param_node.attrib["type"] = param_type

    # set the optional attribute of parameters
    #
    # OpenMS uses sets text, int, select, bool parameters that have a default
    # as optional (required=False), the default value is set implicitly if no
    # value is given.
    # This is reasonable for the CLI because one certainly does not want the
    # user to specify the default manually for all parameters.
    # For Galaxy tools setting these parameters as required leads to the
    # equivalent behavior. Assuming required is better because it makes
    # the implicit setting of parameters more transparent to the user
    # (in Galaxy the default would be prefilled in the form and at least
    # one option needs to be selected).
    if not (param.default is None or type(param.default) is _Null) and param_node.attrib["type"] in ["integer", "float", "text", "boolean", "select"]:
        # logger.error("%s %s %s %s %s" % (param.name, param.default is None, type(param.default) is _Null, param_type, param.type))
        optional = False
    else:
        optional = not param.required
    param_node.attrib["optional"] = str(optional).lower()

    if is_selection_parameter(param):
        if param.is_list:
            param_node.attrib["multiple"] = "true"

    # check for parameters with restricted values (which will correspond to a "select" in galaxy)
    if param.restrictions is not None or param_type == "boolean":
        # it could be either _Choices or _NumericRange, with special case for boolean types
        if param_type == "boolean":
            create_boolean_parameter(param_node, param)
        elif type(param.restrictions) is _Choices:
            # create as many <option> elements as restriction values
            if is_out_type_param(param, model):
                formats = get_formats(param, model, o2g)
                for fmt in formats:
                    option_node = add_child_node(param_node, "option",
                                                 OrderedDict([("value", str(fmt))]))
                    option_node.text = o2g[str(fmt)]
                    if fmt.lower() != o2g[str(fmt)]:
                        option_node.text += " (%s)" % fmt
                    if is_default(fmt, param):
                        option_node.attrib["selected"] = "true"
            else:
                unique_choices = set()
                for choice in param.restrictions.choices:
                    if str(choice) in unique_choices:
                        logging.warning(f"Option {choice} of {utils.extract_param_name(param)} is not unique.")
                        continue
                    else:
                        unique_choices.add(str(choice))
                    option_node = add_child_node(param_node, "option",
                                                 OrderedDict([("value", str(choice))]),
                                                 text=str(choice))
                    if is_default(choice, param):
                        option_node.attrib["selected"] = "true"

            # add validator to check that "nothing selected" is not seletcedto mandatory options w/o default
            if param_node.attrib["optional"] == "False" and (param.default is None or type(param.default) is _Null):
                validator_node = add_child_node(param_node, "validator", OrderedDict([("type", "expression"), ("message", "A value needs to be selected")]))
                validator_node.text = 'value != "select a value"'

        # numeric ranges (which appear for int and float ITEMS and ITEMLISTS)
        # these are reflected by min and max attributes
        # since item lists become text parameters + validator these don't need these attributes
        elif type(param.restrictions) is _NumericRange and param_type == "text":
            pass
        elif type(param.restrictions) is _NumericRange and param_type != "text":
            if param.type is not int and param.type is not float:
                raise InvalidModelException("Expected either 'int' or 'float' in the numeric range restriction for "
                                            "parameter [%(name)s], but instead got [%(type)s]" %
                                            {"name": param.name, "type": type(param.restrictions)})
            # extract the min and max values and add them as attributes
            # validate the provided min and max values
            if param.restrictions.n_min is not None:
                param_node.attrib["min"] = str(param.restrictions.n_min)
            if param.restrictions.n_max is not None:
                param_node.attrib["max"] = str(param.restrictions.n_max)
        elif type(param.restrictions) is _FileFormat:
            # has already been handled
            pass
        else:
            raise InvalidModelException("Unrecognized restriction type [%(type)s] for parameter [%(name)s]"
                                        % {"type": type(param.restrictions), "name": param.name})

    if param_type == "text":
        # for repeats (which are rendered as text field in the tool form) that are actually
        # integer/floats special validation is necessary (try to convert them and check if
        # in the min max range if a range is given)
        if TYPE_TO_GALAXY_TYPE[param.type] in ["integer", "float"]:
            valsan = expand_macro(param_node,
                                  "list_%s_valsan" % TYPE_TO_GALAXY_TYPE[param.type],
                                  dict([("name", get_galaxy_parameter_name(param))]))

            if type(param.restrictions) is _NumericRange and not (param.restrictions.n_min is None and param.restrictions.n_max is None):
                expression = "len(value.split(' ')) == len([_ for _ in value.split(' ') if "
                message = "a space separated list of %s values " % TYPE_TO_GALAXY_TYPE[param.type]

                if param.restrictions.n_min is not None and param.restrictions.n_max is not None:
                    expression += f" {param.restrictions.n_min} <= {param.type.__name__}(_) <= {param.restrictions.n_max}"
                    message += f"in the range {param.restrictions.n_min}:{param.restrictions.n_max} "
                elif param.restrictions.n_min is not None:
                    expression += f" {param.restrictions.n_min} <= {param.type.__name__}(_)"
                    message += "in the range %s: " % (param.restrictions.n_min)
                elif param.restrictions.n_max is not None:
                    expression += f" {param.type.__name__}(_) <= {param.restrictions.n_max}"
                    message += "in the range :%s " % (param.restrictions.n_min)
                expression += "])\n"
                message += "is required"
                validator_node = SubElement(valsan, "validator", OrderedDict([("type", "expression"), ("message", message)]))
                validator_node.text = CDATA(expression)
        else:
            # add quotes to the default values (only if they include spaces .. then the UI looks nicer)
            if not (param.default is None or type(param.default) is _Null) and param.type is not _InFile:
                if type(param.default) is list:
                    for i, d in enumerate(param.default):
                        if " " in d:
                            param.default[i] = '"%s"' % d
#                 elif " " in param.default:
#                     param.default = '"%s"' %param.default
    # add sanitizer nodes to
    # - text (only those that are not actually integer selects which are treated above) and
    # - select params,
    # this is needed for special character like "[" which are used for example by FeatureFinderMultiplex
    if ((param_type == "text" and not TYPE_TO_GALAXY_TYPE[param.type] in ["integer", "float"]) or is_selection_parameter(param)) and param.type is not _InFile:
        if param.is_list and not is_selection_parameter(param):
            valsan = expand_macro(param_node, "list_string_val",
                                  dict([("name", get_galaxy_parameter_name(param))]))
        valsan = expand_macro(param_node, "list_string_san",
                              dict([("name", get_galaxy_parameter_name(param))]))

    # check for default value
    if not (param.default is None or type(param.default) is _Null):
        # defaults of selects are set via the selected attribute of the options (happens above)
        if param_type == "select":
            pass
        elif type(param.default) is list:
            # we ASSUME that a list of parameters looks like:
            # $ tool -ignore He Ar Xe
            # meaning, that, for example, Helium, Argon and Xenon will be ignored
            param_node.attrib["value"] = ' '.join(map(str, param.default))

        elif param_type != "boolean":
            param_node.attrib["value"] = str(param.default)
        else:
            # simple boolean with a default
            if param.default is True:
                param_node.attrib["checked"] = "true"
    elif param.type is int or param.type is float or param.type is str:
        if param_type == "select":
            pass
        else:
            param_node.attrib["value"] = ""

    # add label, help, and argument
    label = "%s parameter" % param.name
    help_text = ""

    if param.description is not None:
        label, help_text = generate_label_and_help(param.description)
    if param.is_list and not is_selection_parameter(param) and param.type is not _InFile:
        help_text += " (space separated list, in order to allow for spaces in list items surround them by single quotes)"
    if param.type is _InFile:
        help_text += " select %s data sets(s)" % (",".join(get_galaxy_formats(param, model, o2g, TYPE_TO_GALAXY_TYPE[_InFile])))

    param_node.attrib["label"] = label
    param_node.attrib["help"] = help_text

    hardcoded_attributes = parameter_hardcoder.get_hardcoded_attributes(param.name, model.name, 'XML')
    if hardcoded_attributes is not None:
        for a in hardcoded_attributes:
            param_node.attrib[a] = str(hardcoded_attributes[a])

    if param_node.attrib["type"] == "data" and param_node.attrib.get("multiple") == "true":
        conditional_attrib = OrderedDict([
            ("name", get_galaxy_parameter_name(param) + "_cond")
        ])
        conditional = Element("conditional", conditional_attrib)

        select_attrib = OrderedDict([
            ("name", get_galaxy_parameter_name(param) + "_select"),
            ("type", "select"),
            ("label", f"Run tool in batch mode for -{get_galaxy_parameter_name(param)}")
        ])
        select = add_child_node(conditional, "param", select_attrib)
        add_child_node(select, "option", attributes={"value": "no"}, text="No: process all datasets jointly")
        add_child_node(select, "option", attributes={"value": "yes"}, text="Yes: process each dataset in an independent job")

        when_no = add_child_node(conditional, "when", attributes={"value": "no"})
        when_no.append(copy.deepcopy(param_node))
        when_yes = add_child_node(conditional, "when", attributes={"value": "yes"})
        param_node.attrib["multiple"] = "false"
        when_yes.append(param_node)
        return conditional

    return param_node


def generate_label_and_help(desc):
    help_text = ""
    # This tag is found in some descriptions
    if not isinstance(desc, str):
        desc = str(desc)
#     desc = desc.encode("utf8")
    desc = desc.replace("#br#", ". ")
    # Get rid of dots in the end
    if desc.endswith("."):
        desc = desc.rstrip(".")
    # Check if first word is a normal word and make it uppercase
    if str(desc).find(" ") > -1:
        first_word, rest = str(desc).split(" ", 1)
        if str(first_word).islower():
            # check if label has a quotient of the form a/b
            if first_word.find("/") != 1:
                first_word.capitalize()
        desc = first_word + " " + rest
#     label = desc.decode("utf8")
    label = desc

    # split delimiters ".,?!;("
    if len(desc) > 50:
        m = re.search(r"([.?!] |e\.g\.|\(e\.g\.|i\.e\.|\(i\.e\.)", desc)
        if m is not None:
            label = desc[:m.start()].rstrip(".?!, ")
            help_text = desc[m.start():].lstrip(".?!, ")

#     # Try to split the label if it is too long
#     if len(desc) > 50:
#         # find an example and put everything before in the label and the e.g. in the help
#         if desc.find("e.g.") > 1 :
#             label, help_text = desc.split("e.g.",1)
#             help_text = "e.g." + help_text
#         else:
#             # find the end of the first sentence
#             # look for ". " because some labels contain .file or something similar
#             delimiter = ""
#             if desc.find(". ") > 1 and desc.find("? ") > 1:
#                 if desc.find(". ") < desc.find("? "):
#                     delimiter = ". "
#                 else:
#                     delimiter = "? "
#             elif desc.find(". ") > 1:
#                 delimiter = ". "
#             elif desc.find("? ") > 1:
#                 delimiter = "? "
#             if delimiter != "":
#                 label, help_text = desc.split(delimiter, 1)
#
#             # add the question mark back
#             if delimiter == "? ":
#                 label += "? "

    # remove all linebreaks
    label = label.rstrip().rstrip('<br>').rstrip()
    return label, help_text


def is_boolean_parameter(param):
    """
    determines if the given choices are boolean (basically, if the possible values are true/false)
    @param param the ctd parameter
    @return True iff a boolean parameter
    """
    # detect boolean selects of OpenMS
    if type(param.restrictions) is _Choices:
        return set(param.restrictions.choices) == {"true", "false"}
    else:
        return param.type is bool


def is_selection_parameter(param):
    """
    determines if there are choices for the parameter and its not bool
    @param param the ctd parameter
    @return True iff a selection parameter
    """
    if type(param.restrictions) is _Choices:
        return set(param.restrictions.choices) != {"true", "false"}
    else:
        return False


def get_lowercase_list(some_list):
    return [str(_).lower().strip() for _ in some_list]


def create_boolean_parameter(param_node, param):
    """
    creates a galaxy boolean parameter type
    this method assumes that param has restrictions, and that only two restictions are present
    (either yes/no or true/false)

    TODO: true and false values can be way more than 'true' and 'false'
        but for that we need CTD support
    """

    # in ctd (1.6.2) bools are strings with restriction true,false
    # - if the default is false then they are flags
    # - otherwise the true or false value needs to be added (where the true case is unnecessary)
    # A special case are restrictions false,true which are not treated as flags
    if param.type == str:
        choices = get_lowercase_list(param.restrictions.choices)
        if set(choices) == {"true", "false"}:
            param_node.attrib["truevalue"] = "true"
            param_node.attrib["falsevalue"] = "false"
        else:
            param_node.attrib["truevalue"] = choices[0]
            param_node.attrib["falsevalue"] = choices[1]

        # set the checked attribute
        if param.default is not None:
            checked_value = "false"
            default = param.default.lower().strip()
            if default == "yes" or default == "true":
                checked_value = "true"
            param_node.attrib["checked"] = checked_value
    else:
        param_node.attrib["truevalue"] = "true"
        param_node.attrib["falsevalue"] = "false"
        param_node.attrib["checked"] = str(param.default).lower()

    if "optional" in param_node.attrib:
        del param_node.attrib["optional"]


def all_outputs(model, parameter_hardcoder):
    """
    return lists of required and optional output parameters
    """
    out = []
    optout = []
    for param in utils.extract_and_flatten_parameters(model):
        hardcoded_value = parameter_hardcoder.get_hardcoded_value(utils.extract_param_name(param), model.name)
        if parameter_hardcoder.get_blacklist(utils.extract_param_name(param), model.name) or hardcoded_value:
            # let's not use an extra level of indentation and use NOP
            continue
        if not (param.type is _OutFile or param.type is _OutPrefix):
            continue

        if not param.required:
            optout.append(param)
        else:
            out.append(param)

    return out, optout


def output_filter_text(param):
    """
    get the text or the filter for optional outputs

    """
    return '"%s_FLAG" in OPTIONAL_OUTPUTS' % param.name


def create_outputs(parent, model, **kwargs):
    """
    create outputs section of the Galaxy tool
    @param tool the Galaxy tool
    @param model the ctd model
    @param kwargs
      - parameter_hardcoder and
      - supported_file_formats ()
    """
    outputs_node = add_child_node(parent, "outputs")
    parameter_hardcoder = kwargs["parameter_hardcoder"]

    for param in utils.extract_and_flatten_parameters(model):
        param = modify_param_for_galaxy(param)
        # no need to show hardcoded parameters
        hardcoded_value = parameter_hardcoder.get_hardcoded_value(utils.extract_param_name(param), model.name)
        if parameter_hardcoder.get_blacklist(utils.extract_param_name(param), model.name) or hardcoded_value:
            # let's not use an extra level of indentation and use NOP
            continue
        if param.type is not _OutFile and param.type is not _OutPrefix:
            continue
        create_output_node(outputs_node, param, model, kwargs["supported_file_formats"], parameter_hardcoder)

    # If there are no outputs defined in the ctd the node will have no children
    # and the stdout will be used as output
    out, optout = all_outputs(model, parameter_hardcoder)
    if len(out) == 0:
        stdout = add_child_node(outputs_node, "data",
                                OrderedDict([("name", "stdout"), ("format", "txt"),
                                             ("label", "${tool.name} on ${on_string}: stdout"),
                                             ("format", "txt")]))
        add_child_node(stdout, "filter", text="OPTIONAL_OUTPUTS is None")

    # manually add output for the ctd file
    ctd_out = add_child_node(outputs_node, "data", OrderedDict([("name", "ctd_out"), ("format", "xml"), ("label", "${tool.name} on ${on_string}: ctd")]))
    add_child_node(ctd_out, "filter", text='OPTIONAL_OUTPUTS is not None and "ctd_out_FLAG" in OPTIONAL_OUTPUTS')
    return outputs_node


def create_output_node(parent, param, model, supported_file_formats, parameter_hardcoder):
    g2o, o2g = get_fileformat_maps(supported_file_formats)

    # add a data node / collection + discover_datasets
    # in the former case we just set the discover_node equal to the data node
    # then we can just use this to set the common format attribute
    if not param.is_list and param.type is not _OutPrefix:
        data_node = add_child_node(parent, "data")
        discover_node = data_node
    else:
        data_node = add_child_node(parent, "collection")
        data_node.attrib["type"] = "list"
        discover_node = add_child_node(data_node, "discover_datasets",
                                       OrderedDict([("directory", get_galaxy_parameter_path(param, separator="_")),
                                                    ("recurse", "true")]))

    data_node.attrib["name"] = get_galaxy_parameter_path(param, separator="_")
    data_node.attrib["label"] = "${tool.name} on ${on_string}: %s" % utils.extract_param_name(param)

    formats = get_galaxy_formats(param, model, o2g, TYPE_TO_GALAXY_TYPE[_OutFile])
    type_param = get_out_type_param(param, model, parameter_hardcoder)
    corresponding_input, fmt_from_corresponding = get_corresponding_input(param, model)
    if type_param is not None:
        type_param_name = get_galaxy_parameter_path(type_param)
        type_param_choices = get_formats(param, model, o2g)  # [_ for _ in type_param.restrictions.choices]
    elif len(formats) > 1 and (corresponding_input is None or not
                               fmt_from_corresponding):  # and not param.is_list:
        type_param_name = get_galaxy_parameter_path(param, suffix="type")
        type_param_choices = get_formats(param, model, o2g)
    else:
        type_param_name = None

    # if there is only a single possible output format we set this
#     logging.error("%s %s %s %s %s" %(param.name, formats, type_param, fmt_from_corresponding, corresponding_input))
    if len(formats) == 1:
        logging.info(f"OUTPUT {param.name} 1 fmt {formats}")
        discover_node.attrib["format"] = formats.pop()
        if param.is_list:
            discover_node.attrib["pattern"] = "__name__"
        elif param.type is _OutPrefix:
            discover_node.attrib["pattern"] = r"_?(?P<designation>.*)\.[^.]*"

    # if there is another parameter where the user selects the format
    # then this format was added as file extension on the CLI, now we can discover this
    elif type_param_name is not None:
        logging.info("OUTPUT %s type" % param.name)
        if not param.is_list:
            if len(type_param_choices) > 1:
                change_node = add_child_node(data_node, "change_format")
            for i, r in enumerate(type_param_choices):
                f = o2g.get(r, None)
                # TODO this should not happen for fully specified fileformats file
                if f is None:
                    f = r
                if i == 0:
                    data_node.attrib["format"] = f
                else:
                    add_child_node(change_node, "when", OrderedDict([("input", type_param_name), ("value", r), ("format", f)]))
        else:
            discover_node.attrib["pattern"] = "__name_and_ext__"
    elif corresponding_input is not None:
        logging.info(f"OUTPUT {param.name} input {corresponding_input.name}")
        if param.is_list or param.type is _OutPrefix:
            # TODO like for the else branch format_source would be better:
            # with https://github.com/galaxyproject/galaxy/pull/9493
            discover_node.attrib["pattern"] = "__name_and_ext__"
#             data_node.attrib["structured_like"] = get_galaxy_parameter_name(corresponding_input)
            # data_node.attrib["inherit_format"] = "true"
        else:
            # TODO once https://github.com/galaxyproject/galaxy/pull/9493 we should use get_galaxy_parameter_path
            data_node.attrib["format_source"] = get_galaxy_parameter_name(corresponding_input)
            data_node.attrib["metadata_source"] = get_galaxy_parameter_name(corresponding_input)
    else:
        logging.info("OUTPUT %s else" % (param.name))
        if not param.is_list:
            data_node.attrib["auto_format"] = "true"
        else:
            raise InvalidModelException("No way to know the format for"
                                        "for output [%(name)s]" % {"name": param.name})


#     # data output has fomat (except if fromat_source has been added already)
#     # note .. collection output has no format
#     if not param.is_list and not "format_source" in data_node.attrib:
#         data_node.attrib["format"] = data_format

    # add filter for optional parameters
    if not param.required:
        filter_node = add_child_node(data_node, "filter")
        filter_node.text = "OPTIONAL_OUTPUTS is not None and " + output_filter_text(param)
    return data_node


def get_supported_file_types(formats, supported_file_formats):
    r = set()
    for f in formats:
        if f in supported_file_formats:
            r.add(supported_file_formats[f].galaxy_extension)
    return r
#         print f, f in supported_file_formats, supported_file_formats[f].galaxy_extension
#     return set([supported_file_formats[_].galaxy_extension
#                for _ in formats if _ in supported_file_formats])


def create_change_format_node(parent, data_formats, input_ref):
    #  <change_format>
    #    <when input="secondary_structure" value="true" format="txt"/>
    #  </change_format>
    change_format_node = add_child_node(parent, "change_format")
    for data_format in data_formats:
        add_child_node(change_format_node, "when",
                       OrderedDict([("input", input_ref), ("value", data_format), ("format", data_format)]))


def create_tests(parent, inputs=None, outputs=None):
    """
    create tests section of the Galaxy tool
    @param tool the Galaxy tool
    @param inputs a copy of the inputs
    """

    tests_node = add_child_node(parent, "tests")

    fidx = 0
    test_node = add_child_node(tests_node, "test")
    strip_elements(inputs, "validator", "sanitizer")
    for node in inputs.iter():
        # params do not have a name if redundant with argument
        # -> readd and restore attrib order
        if node.tag == "param" and not node.attrib.get("name") and node.attrib.get("argument"):
            attrib = copy.deepcopy(node.attrib)
            node.attrib["name"] = node.attrib["argument"].lstrip("-").replace("-", "_")
            for a in attrib:
                del node.attrib[a]
                node.attrib[a] = attrib[a]
        # this should be batch mode conditionals
        # select yes/no at random
        if node.tag == "conditional":
            select = node.find("./param")
            options = [x.attrib["value"] for x in select.findall("./option")]
            option = random.choice(options)
            select.attrib["value"] = option
            for when in node.findall(".//when"):
                when.tag = "delete_node"
                if when.attrib["value"] != option:
                    continue
                for p in when:
                    node.append(p)

        if node.tag == "expand" and node.attrib["macro"] == ADVANCED_OPTIONS_NAME + "_macro":
            node.tag = "section"
            node.attrib["name"] = ADVANCED_OPTIONS_NAME
        if "type" not in node.attrib:
            continue

        if node.attrib["type"] == "select" and node.getparent().tag != "conditional" and (("true" in {_.attrib.get("selected", "false") for _ in node}) or (node.attrib.get("value", "") != "")):
            node.tag = "delete_node"
            continue

        # TODO make this optional (ie add aparameter)
        if node.attrib.get("optional", None) == "true" and node.attrib["type"] != "boolean":
            node.tag = "delete_node"
            continue

        if node.attrib["type"] == "boolean":
            if node.attrib["checked"] == "true":
                node.attrib["value"] = "true"  # node.attrib["truevalue"]
            else:
                node.attrib["value"] = "false"  # node.attrib["falsevalue"]
        elif node.attrib["type"] == "text" and node.attrib["value"] == "":
            node.attrib["value"] = "1 2"  # use a space separated list here to cover the repeat (int/float) case
        elif node.attrib["type"] == "integer" and node.attrib["value"] == "":
            node.attrib["value"] = "1"
        elif node.attrib["type"] == "float" and node.attrib["value"] == "":
            node.attrib["value"] = "1.0"
        elif node.attrib["type"] == "select":
            if node.getparent().tag == "conditional":  # set batch mode conditional select, this is done elsewhere
                pass
            elif node.attrib.get("multiple", "false") == "false":
                node.attrib["value"] = node[0].attrib["value"]
            elif node.attrib.get("multiple", None) == "true":
                node.attrib["value"] = ",".join([_.attrib["value"] for _ in node if "value" in _.attrib])
        elif node.attrib["type"] == "data":
            node.attrib["ftype"] = node.attrib["format"].split(',')[0]
            if node.attrib.get("multiple", "false") == "true":
                node.attrib["value"] = "{fidx}test.ext,{fidx}test2.ext".format(fidx=fidx)
            else:
                node.attrib["value"] = f"{fidx}test.ext"
            fidx += 1
    for node in inputs.iter():
        for a in set(node.attrib) - {"name", "value", "ftype"}:
            del node.attrib[a]
    strip_elements(inputs, "delete_node", "option", "expand")
    for node in inputs:
        test_node.append(node)

    outputs_cnt = 0
    for node in outputs.iter():
        if node.tag == "data" or node.tag == "collection":
            # assuming that all filters avaluate to false
            has_filter = False
            for c in node:
                if c.tag == "filter":
                    has_filter = True
                    break
            if not has_filter:
                outputs_cnt += 1
            else:
                node.tag = "delete_node"
        if node.tag == "data":
            node.tag = "output"
            try:
                node.attrib["ftype"] = node.attrib["format"]
            except KeyError:
                pass
            node.attrib["value"] = "outfile.txt"
        if node.tag == "collection":
            node.tag = "output_collection"
            node.attrib["count"] = "0"  # add a mock count element
        if node.attrib.get("name", None) == "stdout":
            node.attrib["lines_diff"] = "2"
        for a in set(node.attrib) - {"name", "value", "ftype", "lines_diff", "count"}:
            del node.attrib[a]
    strip_elements(outputs, "delete_node", "discover_datasets", "filter", "change_format")

    for node in outputs:
        test_node.append(node)
    # if no optional output is selected the stdout is added as output
    if outputs_cnt == 0:
        outputs_cnt = 1
    test_node.attrib["expect_num_outputs"] = str(outputs_cnt)


def create_test_only(model, **kwargs):

    parameter_hardcoder = kwargs["parameter_hardcoder"]
    unsniffable = kwargs["test_unsniffable"]
    supported_file_formats = kwargs["supported_file_formats"]
    g2o, o2g = get_fileformat_maps(supported_file_formats)
    test_condition = [_.split("=") for _ in kwargs["test_condition"]]

    section_nodes = dict()
    section_params = dict()

    test = Element("test")
    advanced = add_child_node(test, "section", OrderedDict([("name", "adv_opts")]))

    optout = ["ctd_out_FLAG"]
    outcnt = 1

    for param in utils.extract_and_flatten_parameters(model, True):
        ext = None
        # no need to show hardcoded parameters
        # except for the test parameter
        hardcoded_value = parameter_hardcoder.get_hardcoded_value(utils.extract_param_name(param), model.name)
        if parameter_hardcoder.get_blacklist(utils.extract_param_name(param), model.name) or hardcoded_value is not None:
            if param.name != "test":
                continue

        # analogous to input generation
        if is_in_type_param(param, model):
            continue

        if utils.extract_param_name(param.parent) in section_nodes:
            parent = section_nodes[utils.extract_param_name(param.parent)]
        elif type(param) is not ParameterGroup and param.advanced:
            parent = advanced
        else:
            parent = test

        if type(param) is ParameterGroup:
            section_params[utils.extract_param_name(param)] = param
            section_nodes[utils.extract_param_name(param)] = add_child_node(parent, "section", OrderedDict([("name", param.name)]))
            continue

        if param.type is _OutFile:
            given = type(param.default) is _OutFile or (type(param.default) is list) and len(param.default) > 0
            if not param.required and given:
                optout.append("%s_FLAG" % param.name)
            if given:
                oms_formats = get_formats(param, model, o2g)
                formats = get_galaxy_formats(param, model, o2g, TYPE_TO_GALAXY_TYPE[_OutFile])
                type_param = get_out_type_param(param, model, parameter_hardcoder)
                corresponding_input, fmt_from_corresponding = get_corresponding_input(param, model)
                if type(param.default) is _OutFile:
                    f = param.default
                elif type(param.default) is list:
                    f = param.default[0]
                else:
                    raise Exception("Outfile with non str or list default {}[{}]".format(param, type(param.default)))
                # get the file type from the longest possible extension that
                # matches the known extensions
                # longest: because e.g. pep.xml should be prefered over xml
                if f.endswith(".tmp"):
                    f = f[:-4]
                splitted = f.split(".")
                ext = None
                for i in range(len(splitted)):
                    check_ext = ".".join(splitted[i:])
                    for oms_ext in sorted(sorted(o2g), key=lambda x: len(x), reverse=True):
                        if check_ext.endswith(f".{oms_ext}") or check_ext.endswith(f"_{oms_ext}"):
                            ext = o2g[oms_ext]
                            break
                    if ext is not None:
                        break
                if ext not in formats:
                    if ext == "txt" and "csv" in formats:
                        ext = "csv"
                    elif ext == "txt" and "tsv" in formats:
                        ext = "tsv"
                    elif len(formats) == 1:
                        ext = formats[0]

                if len(formats) > 1 and (corresponding_input is None or not
                                         fmt_from_corresponding):  # and not param.is_list:
                    if type_param is None:
                        try:
                            attrib = OrderedDict([
                                ("name", f"{param.name}_type"),
                                ("value", get_oms_format(oms_formats, ext, o2g))
                            ])
                            add_child_node(parent, "param", attrib)
                        except KeyError:
                            raise Exception(f"parent {parent} name {param.name} ext {ext}")
                if type_param is not None and type(type_param.default) is _Null:
                    if ext is not None:
                        type_param.default = get_oms_format(oms_formats, ext, o2g)

            if param.required or given:
                outcnt += 1

        # don't output empty values for bool, and data parameters
        if type(param.default) is _Null and not param.required:
            if is_boolean_parameter(param):
                continue
            elif param.type is _OutFile:
                continue
            elif param.type is _InFile:
                continue

        # lists need to be joined appropriately
        # - special care for outfile lists (ie collections): since we do not know (easily) the names of the collection elements we just use the count
        # exception of list parameters that are hardcoded to non-lists (the the default is still a list)
        if not param.is_list and type(param.default) is list:
            logging.info("Found non-list parameter %s with list default (hardcoded?). Using only first value/" % param.name)
            try:
                param.default = param.default[0]
            except KeyError:
                param.default = _Null()

        if (is_selection_parameter(param) or param.is_list) and type(param.default) is _Null:
            value = None
        elif param.is_list and type(param.default) is not _Null:
            if param.type is _InFile:
                value = ','.join(map(str, param.default))
            elif param.type is _OutFile:
                value = str(len(param.default))
            elif param.type is str:
                if type(param.restrictions) is _Choices:
                    value = ','.join(map(str, param.default))
                else:
                    value = '"' + '" "'.join(map(str, param.default)) + '"'
            else:
                value = ' '.join(map(str, param.default))
        else:
            if type(param.default) is bool:
                value = str(param.default).lower()
            else:
                value = str(param.default)

        # for input-files with multiple=true we need to add the batch mode conditional
        if param.type is _InFile and param.is_list:
            batch_cond_attrib = OrderedDict([
                ("name", get_galaxy_parameter_name(param) + "_cond")
            ])
            parent = add_child_node(parent, "conditional", batch_cond_attrib)

        # use name where dashes are replaced by underscores
        # see also create inputs
        if param.type is _OutFile:
            name = get_galaxy_parameter_path(param, separator="_")
            if param.is_list:
                nd = add_child_node(test, "output_collection", OrderedDict([("name", name), ("count", value)]))
            else:
                if value:
                    # nd = add_child_node(test, "output", OrderedDict([("name", name), ("location", f"@TEST_DATA_LOCATION@{value}")]))
                    nd = add_child_node(test, "output", OrderedDict([("name", name), ("value", value)]))
                    for tc in test_condition:
                        nd.attrib[tc[0]] = tc[1]
                    if ext:
                        nd.attrib["ftype"] = ext
        elif param.type is _OutPrefix:
            # #for outprefix elements / count need to be added manually
            name = get_galaxy_parameter_path(param, separator="_")
            nd = add_child_node(test, "output_collection", OrderedDict([("name", name), ("count", "")]))
        else:
            name = get_galaxy_parameter_name(param)
            if value is not None:
                nd = add_child_node(parent, "param", OrderedDict([("name", name), ("value", value)]))
                # if param.type is _InFile:
                #     nd.attrib["location"] = "@TEST_DATA_LOCATION@" + nd.attrib["value"]

        # add format attribute for unsniffable extensions
        if param.type is _InFile:
            ext = os.path.splitext(value)[1][1:]
            if ext in unsniffable and ext in o2g:
                nd.attrib["ftype"] = o2g[ext]



    add_child_node(test, "param", OrderedDict([("name", "OPTIONAL_OUTPUTS"),
                                               ("value", ",".join(optout))]))
    ctd_out = add_child_node(test, "output", OrderedDict([("name", "ctd_out"), ("ftype", "xml")]))
    ctd_assert = add_child_node(ctd_out, "assert_contents")
    add_child_node(ctd_assert, "is_valid_xml")

    if outcnt == 0:
        outcnt += 1
        nd = add_child_node(test, "output", OrderedDict([("name", "stdout"),
                                                         ("value", "stdout.txt"),
                                                         ("compare", "sim_size")]))
    
    # add a stdout assertion
    stdout_assert = add_child_node(test, "assert_stdout")
    stdout_has_text = add_child_node(stdout_assert, "has_text_matching", OrderedDict([("expression", "@EXECUTABLE@ took .* \(wall\), .* \(CPU\), .* \(system\), .* \(user\)(; Peak Memory Usage: 32 MB)?.")]))

    test.attrib["expect_num_outputs"] = str(outcnt)
#     if all_optional_outputs(model, parameter_hardcoder):
    return test


def create_help(tool, model):
    """
    create help section of the Galaxy tool
    @param tool the Galaxy tool
    @param model the ctd model
    @param kwargs
    """
    help_node = add_child_node(tool, "help")
    help_node.text = CDATA(utils.extract_tool_help_text(model))


def add_child_node(parent_node, child_node_name, attributes=OrderedDict([]), text=None):
    """
    helper function to add a child node using the given name to the given parent node
    @param parent_node the parent
    @param child_node_name the desired name of the child
    @param attributes desired attributes of the child
    @return the created child node
    """
    child_node = SubElement(parent_node, child_node_name, attributes)
    if text is not None:
        child_node.text = text
    return child_node
