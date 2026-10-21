from importlib import metadata

import ryaml
import textlibs

textlibs.run("ryaml", metadata.version("ryaml"), load_all=ryaml.loads_all, dump=ryaml.dumps,
             own_errors=(ryaml.InvalidYamlError,))
