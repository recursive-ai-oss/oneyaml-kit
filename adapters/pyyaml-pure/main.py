import pyyaml_common
import yaml

pyyaml_common.run("pyyaml-pure", yaml.SafeLoader, yaml.SafeDumper, with_libyaml=False)
