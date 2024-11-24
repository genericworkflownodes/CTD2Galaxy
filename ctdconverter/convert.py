import logging
import os
import sys
import traceback
from argparse import (
    ArgumentParser,
    RawDescriptionHelpFormatter
)

from . import (
    __updated__,
    __version__
)
from .common import utils
from .common.exceptions import (
    ApplicationException,
    ModelError
)

program_version = "v%s" % __version__
program_build_date = str(__updated__)
program_version_message = f'%(prog)s {program_version} ({program_build_date})'
program_short_description = "CTDConverter - A project from the WorkflowConversion family " \
                            "(https://github.com/WorkflowConversion/CTDConverter)"
program_usage = '''
USAGE:

  $ python convert.py [FORMAT] [ARGUMENTS ...]

FORMAT can be either one of the supported output formats: cwl, galaxy.

There is one converter for each supported FORMAT, each taking a different set of arguments. Please consult the detailed
documentation for each of the converters. Nevertheless, all converters have the following common parameters/options:


I - Parsing a single CTD file and convert it:

  $ python convert.py [FORMAT] -i [INPUT_FILE] -o [OUTPUT_FILE]


II - Parsing several CTD files, output converted wrappers in a given folder:

  $ python converter.py [FORMAT] -i [INPUT_FILES] -o [OUTPUT_DIRECTORY]


For more detailed help see README.md in the root folder as well as `galaxy/README.md` or `cwl/README.md`.
'''

program_license = '''{short_description}

Copyright 2017, WorklfowConversion

Licensed under the Apache License, Version 2.0 (the "License");
you may not use this file except in compliance with the License.
You may obtain a copy of the License at

    http://www.apache.org/licenses/LICENSE-2.0

Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
See the License for the specific language governing permissions and
limitations under the License.

{usage}
'''.format(short_description=program_short_description, usage=program_usage)


def main(argv=None):
    if argv is None:
        argv = sys.argv
    else:
        sys.argv.extend(argv)

    # check that we have, at least, one argument provided
    # at this point we cannot parse the arguments, because each converter takes different arguments, meaning each
    # converter will register its own parameters after we've registered the basic ones... we have to do it old school
    if len(argv) < 2:
        logging.error("Not enough arguments provided")
        print("\nUsage: $ CTDConverter [TARGET] [ARGUMENTS]\n\n"
              "Where:\n"
              "  target: one of 'cwl' or 'galaxy'\n\n"
              "Run again using the -h/--help option to print more detailed help.\n")
        return 1

    # TODO: at some point this should look like real software engineering and use a map containing converter instances
    # whose keys would be the name of the converter (e.g., cwl, galaxy), but for the time being, only two formats
    # are supported
    target = str.lower(argv[1])
    if target == 'cwl':
        from .cwl import converter
    elif target == 'galaxy':
        from .galaxy import converter
#     elif target == '-h' or target == '--help' or target == '--h' or target == 'help':
#         print(program_license)
#         return 0
    else:
        logging.error("Unrecognized target engine. Supported targets are 'cwl' and 'galaxy'.")
        return 1

    logging.info("Using %s converter" % target)

    try:
        # Setup argument parser
        parser = ArgumentParser(prog="CTDConverter", description=program_license,
                                formatter_class=RawDescriptionHelpFormatter, add_help=True)
        utils.add_common_parameters(parser, program_version_message, program_build_date)

        # add tool-specific arguments
        converter.add_specific_args(parser)

        # parse arguments and perform some basic, common validation
        args = parser.parse_args()
        validate_and_prepare_common_arguments(args)

        # parse the input CTD files into CTDModels
        parsed_ctds = utils.parse_input_ctds(args.xsd_location, args.input_files, args.output_destination,
                                             converter.get_preferred_file_extension())

        # let the converter do its own thing
        converter.convert_models(args, parsed_ctds)
        return 0

    except KeyboardInterrupt:
        print("Interrupted...")
        return 0

    except ApplicationException as e:
        traceback.print_exc()
        logging.error("CTDConverter could not complete the requested operation.")
        logging.error("Reason: " + e.msg)
        return 1

    except ModelError as e:
        traceback.print_exc()
        logging.error("There seems to be a problem with one of your input CTDs.")
        logging.error("Reason: " + e.msg)
        return 1

    except Exception as e:
        traceback.print_exc()
        logging.error("CTDConverter could not complete the requested operation.")
        logging.error("Reason: " + e.msg)
        return 2


def validate_and_prepare_common_arguments(args):
    # flatten lists of lists to a list containing elements
    lists_to_flatten = ["input_files"]
    for list_to_flatten in lists_to_flatten:
        utils.flatten_list_of_lists(args, list_to_flatten)

    # if input is a single file, we expect output to be a file (and not a dir that already exists)
    if len(args.input_files) == 1:
        if os.path.isdir(args.output_destination):
            raise ApplicationException("If a single input file is provided, output (%s) is expected to be a file "
                                       "and not a folder.\n" % args.output_destination)

    # if input is a list of files, we expect output to be a folder
    if len(args.input_files) > 1:
        if not os.path.isdir(args.output_destination):
            raise ApplicationException("If several input files are provided, output (%s) is expected to be an "
                                       "existing directory.\n" % args.output_destination)

    # check that the provided input files, if provided, contain a valid file path
    input_arguments_to_check = ["xsd_location", "input_files", "hardcoded_parameters"]
    for argument_name in input_arguments_to_check:
        utils.validate_argument_is_valid_path(args, argument_name)

    # add the parameter hardcoder
    args.parameter_hardcoder = utils.parse_hardcoded_parameters(args.hardcoded_parameters)


if __name__ == "__main__":
    sys.exit(main())
