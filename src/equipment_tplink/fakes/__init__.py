"""Look-alikes of the python-kasa and tplink-cloud-api surfaces the drivers use.

They back ``serve --fake`` and the test suite. They mimic the libraries' public
behaviour, including their limitations (FINDINGS), and raise the libraries' own
exception types so the real drivers run unchanged against them.
"""
