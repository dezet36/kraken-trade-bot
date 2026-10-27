import os
import sys
import tempfile

from cryptography.fernet import Fernet

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

# Настройки — до первого импорта copier: config читается при импорте.
_TMP = tempfile.mkdtemp(prefix='copier_test_')
os.environ['COPIER_DATA_DIR'] = _TMP
os.environ['COPIER_ENGINE_DATA_DIR'] = os.path.join(_TMP, 'engine')
os.environ['COPIER_SECRET_KEY'] = Fernet.generate_key().decode()
os.environ['COPIER_TG_TOKEN'] = ''
os.environ['COPIER_ADMIN_IDS'] = '111'
os.environ['COPIER_ADMIN_TOKEN'] = 'test-admin-token'
