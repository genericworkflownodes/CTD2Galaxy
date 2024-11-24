#!/usr/bin/env python

# instead of using cwlgen, we decided to use PyYAML directly
# we promptly found a problem with cwlgen, namely, it is not possible to construct something like:
# some_paramter:
#   type: ['null', string]
# which kind of sucks, because this seems to be the way to state that a parameter is truly optional and has no default
# since cwlgen is just "fancy classes" around the yaml.dump() method, we implemented our own generation of yaml

import logging
import ruamel.yaml as yaml
from CTDopts.CTDopts import (
    _Choices,
    _InFile,
    _Null,
    _OutFile
)

from .. import __version__ as version
from ..common import utils

# all cwl-related properties are defined here

CWL_SHEBANG = "#!/usr/bin/env cwl-runner"
CURRENT_CWL_VERSION = 'v1.0'
CWL_VERSION = 'cwlVersion'
CLASS = 'class'
BASE_COMMAND = 'baseCommand'
INPUTS = 'inputs'
ID = 'id'
TYPE = 'type'
INPUT_BINDING = 'inputBinding'
OUTPUT_BINDING = 'outputBinding'
PREFIX = 'prefix'
OUTPUTS = 'outputs'
POSITION = 'position'
VALUE_FROM = 'valueFrom'
GLOB = 'glob'
LABEL = 'label'
DOC = 'doc'
DEFAULT = 'default'

# types
TYPE_NULL = 'null'
TYPE_BOOLEAN = 'boolean'
TYPE_INT = 'int'
TYPE_LONG = 'long'
TYPE_FLOAT = 'float'
TYPE_DOUBLE = 'double'
TYPE_STRING = 'string'
TYPE_FILE = 'File'
TYPE_DIRECTORY = 'Directory'

TYPE_TO_CWL_TYPE = {int: TYPE_INT, float: TYPE_DOUBLE, str: TYPE_STRING, bool: TYPE_BOOLEAN, _InFile: TYPE_FILE,
                    _OutFile: TYPE_FILE, _Choices: TYPE_STRING}


def add_specific_args(parser):
    # no specific arguments for CWL conversion, for now
    # however, this method has to be defined, otherwise ../convert.py won't work for CWL
    pass


def get_preferred_file_extension():
    return "cwl"


def convert_models(args, parsed_ctds):
    """Go through each CTD model and perform the conversion."""
    for parsed_ctd in parsed_ctds:
        model = parsed_ctd.ctd_model
        origin_file = parsed_ctd.input_file
        output_file = parsed_ctd.suggested_output_file

        logging.info("Converting {} (source {})".format(model.name, utils.get_filename(origin_file)))
        cwl_tool = convert_to_cwl(model, args)

        logging.info("Writing to %s" % utils.get_filename(output_file))

        stream = open(output_file, 'w')
        stream.write(CWL_SHEBANG + '\n\n')
        stream.write(f"# This CWL file was automatically generated using CTDConverter version {version}.\n")
        stream.write("# Visit https://github.com/WorkflowConversion/CTDConverter for more information.\n\n")
        yaml.dump(cwl_tool, stream, default_flow_style=False)
        stream.close()


# returns a dictionary
def convert_to_cwl(ctd_model, args):
    # create cwl_tool object with the basic information
    base_command = utils.extract_tool_executable_path(ctd_model, args.default_executable_path)

    # add basic properties
    cwl_tool = {}
    cwl_tool[CWL_VERSION] = CURRENT_CWL_VERSION
    cwl_tool[CLASS] = 'CommandLineTool'
    cwl_tool[LABEL] = ctd_model.opt_attribs["description"]
    cwl_tool[DOC] = utils.extract_tool_help_text(ctd_model)
    cwl_tool[BASE_COMMAND] = base_command

    # TODO: test with optional output files

    # add inputs/outputs
    for param in utils.extract_and_flatten_parameters(ctd_model):

        param_name = utils.extract_param_name(param)
        cwl_fixed_param_name = fix_param_name(param_name)
        hardcoded_value = args.parameter_hardcoder.get_hardcoded_value(param_name, ctd_model.name)
        param_default = str(param.default) if param.default is not _Null and param.default is not None else None

        if param.type is _OutFile:
            create_lists_if_missing(cwl_tool, [INPUTS, OUTPUTS])
            # we know the only outputs are of type _OutFile
            # we need an input of type string that will contain the name of the output file
            label = "Filename for %s output file" % param_name
            input_name_for_output_filename = get_input_name_for_output_filename(param)
            input_param = {}
            input_param[ID] = input_name_for_output_filename
            input_param[DOC] = label
            input_param[LABEL] = label
            if param_default is not None:
                input_param[DEFAULT] = param_default
            input_param[TYPE] = generate_cwl_param_type(param, TYPE_STRING)
            insert_input_binding(ctd_model, param, hardcoded_value, input_param)

            output_binding = {}
            output_binding[GLOB] = "$(inputs.%s)" % input_name_for_output_filename

            output_param = {}
            output_param[ID] = cwl_fixed_param_name
            output_param[OUTPUT_BINDING] = output_binding
            output_param[DOC] = param.description
            output_param[LABEL] = param.description
            output_param[TYPE] = generate_cwl_param_type(param)

            cwl_tool[INPUTS].append(input_param)
            cwl_tool[OUTPUTS].append(output_param)

        else:
            create_lists_if_missing(cwl_tool, [INPUTS])
            # we know that anything that is not an _OutFile is an input
            input_param = {}
            input_param[ID] = cwl_fixed_param_name
            input_param[DOC] = param.description
            input_param[LABEL] = param.description
            if param_default is not None:
                input_param[DEFAULT] = param_default
            input_param[TYPE] = generate_cwl_param_type(param)
            insert_input_binding(ctd_model, param, hardcoded_value, input_param)

            cwl_tool[INPUTS].append(input_param)

    return cwl_tool


def create_lists_if_missing(cwl_tool, keys):
    for key in keys:
        if key not in cwl_tool:
            cwl_tool[key] = []


def get_input_name_for_output_filename(param):
    assert param.type is _OutFile, "Only output files can get a generated filename input parameter."
    return fix_param_name(utils.extract_param_name(param)) + "_filename"


def fix_param_name(param_name):
    # IMPORTANT: there seems to be a problem in CWL if the prefix and the parameter name are the same, so we need to
    #            prepend something to the parameter name that will be registered in CWL, also, using colons in parameter
    #            names seems to bring all sorts of problems for cwl-runner
    return 'param_' + param_name.replace(":", "_")


# in order to provide "true" optional params, the parameter type should be something like ['null', <CWLType>],
# for instance ['null', int]
def generate_cwl_param_type(param, forced_type=None):
    cwl_type = TYPE_TO_CWL_TYPE[param.type] if forced_type is None else forced_type
    return cwl_type if param.required else ['null', cwl_type]


# generate, and insert, the inputBinding
def insert_input_binding(ctd_model, param, hardcoded_value, cwl_input_param):
    prefix = utils.extract_command_line_prefix(param, ctd_model)
    prefix = None if prefix is None or not prefix.strip() else prefix

    input_binding = {}

    if prefix is not None:
        input_binding[PREFIX] = prefix

    if hardcoded_value is not None:
        input_binding[VALUE_FROM] = hardcoded_value

    if param.is_positional():
        input_binding[POSITION] = param.position

    # insert input binding if there's something in it
    if input_binding:
        cwl_input_param[INPUT_BINDING] = input_binding
