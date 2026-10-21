import pyyaml_common
import yaml

pyyaml_common.run("pyyaml-c", yaml.CSafeLoader, yaml.CSafeDumper, with_libyaml=True)
