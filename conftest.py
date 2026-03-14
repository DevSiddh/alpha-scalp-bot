# conftest.py — suppress pandas-ta deprecation noise on Python 3.12+
import warnings
warnings.filterwarnings("ignore", category=DeprecationWarning, module="pandas_ta")
warnings.filterwarnings("ignore", category=FutureWarning, module="pandas_ta")
warnings.filterwarnings("ignore", message=".*DataFrame.swapaxes.*")
warnings.filterwarnings("ignore", message=".*np.bool.*")
warnings.filterwarnings("ignore", message=".*np.int.*")
warnings.filterwarnings("ignore", message=".*np.float.*")
warnings.filterwarnings("ignore", message=".*mode.copy_on_write.*")
