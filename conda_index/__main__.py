"""
Non-debugging `python -m conda_index` entry point (compare with
`conda_index.index.__main__`); same as `conda-index` console_scripts entry
point.
"""

import conda_index.cli.main_index

conda_index.cli.main_index.main()
