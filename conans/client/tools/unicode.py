# Global utilities to support unicode operations
# These tools exist to bridge the gap between Python 2 and 3, since
# Python 3 handles Unicode very well, but Python 2 doesn't, and
# the two do not have consistent mechanisms with which to fix this
import imp
from io import open

# Alternate implementation of imp.load_source which
# uses open() and imp.new_module() instead. This is to work around the fact that
# Python 2's imp.load_source() does not support Unicode. See http://bugs.python.org/issue9425
def load_source(name, pathname):
    try:
        return imp.load_source(name, pathname)
    except UnicodeEncodeError:
        # fallback to open()/new_module
        with open(pathname, encoding='utf8') as f:
            module = imp.new_module(name)
            module.__file__ = pathname
            exec(f.read(), module.__dict__)
            return module

