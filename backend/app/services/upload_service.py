import os
import shutil
import zipfile
import logging
from datetime import datetime
from werkzeug.utils import secure_filename

from app import paths
from app.utils.slug import slugify

logger = logging.getLogger(__name__)

# Maximum upload size is enforced by Flask config (100 MB), but we keep a
# service-level guard too so callers can reason about limits independently.
MAX_UPLOAD_SIZE = 100 * 1024 * 1024

VALID_APP_TYPES = {'docker', 'flask', 'django', 'php', 'static', 'node'}


def detect_app_type(directory):
    """Inspect extracted archive contents and guess the runtime."""
    if os.path.exists(os.path.join(directory, 'docker-compose.yml')):
        return 'docker'
    if os.path.exists(os.path.join(directory, 'Dockerfile')):
        return 'docker'
    if os.path.exists(os.path.join(directory, 'manage.py')):
        return 'django'
    if os.path.exists(os.path.join(directory, 'app.py')) or os.path.exists(os.path.join(directory, 'wsgi.py')):
        return 'flask'
    if os.path.exists(os.path.join(directory, 'package.json')):
        return 'node'
    if os.path.exists(os.path.join(directory, 'index.html')):
        return 'static'
    return 'docker'


def validate_zip(zippath):
    """Validate a service zip archive before extraction.

    Returns a dict with success/error, total size, and file count.
    """
    try:
        with zipfile.ZipFile(zippath, 'r') as zf:
            infos = zf.infolist()
            total_size = 0
            file_count = 0
            for info in infos:
                # Zip-slip protection
                if info.filename.startswith('/') or '..' in info.filename.split('/'):
                    return {'success': False, 'error': f'Unsafe path in archive: {info.filename}'}
                total_size += info.file_size
                file_count += 1
                if total_size > MAX_UPLOAD_SIZE:
                    return {'success': False, 'error': 'Archive contents exceed maximum upload size'}
            return {'success': True, 'total_size': total_size, 'file_count': file_count}
    except zipfile.BadZipFile:
        return {'success': False, 'error': 'Invalid or corrupted zip file'}
    except Exception as e:
        return {'success': False, 'error': str(e)}


def get_app_storage_dir(name):
    """Return the ServerKit-managed storage directory for an uploaded app."""
    base = os.path.abspath(paths.APPS_DIR)
    app_dir = os.path.abspath(os.path.join(base, name))
    if app_dir != base and app_dir.startswith(base + os.sep):
        return app_dir
    raise ValueError('Invalid application name')


def ensure_app_dirs(name):
    app_dir = get_app_storage_dir(name)
    for sub in ('uploads', 'versions', 'backups'):
        os.makedirs(os.path.join(app_dir, sub), exist_ok=True)
    return app_dir


def extract_version(app_dir, zippath, version):
    """Extract a zip into versions/v<N>."""
    version_dir = os.path.join(app_dir, 'versions', f'v{version}')
    if os.path.exists(version_dir):
        shutil.rmtree(version_dir)
    os.makedirs(version_dir, exist_ok=True)

    with zipfile.ZipFile(zippath, 'r') as zf:
        # If every top-level entry shares a single folder, strip it so the
        # version directory contains the app files directly. Only a real
        # folder counts: an archive holding one file (a lone
        # docker-compose.yml) has that file as its common path, and treating
        # it as a folder used to strip it to nothing.
        names = zf.namelist()
        top = os.path.commonpath(names).split('/')[0] if names else ''
        has_top_folder = top and all(n.startswith(top + '/') for n in names)

        if has_top_folder:
            for info in zf.infolist():
                if info.is_dir():
                    continue
                rel = info.filename[len(top):].lstrip('/')
                if not rel:
                    continue
                dest = os.path.join(version_dir, rel)
                os.makedirs(os.path.dirname(dest), exist_ok=True)
                with zf.open(info) as src, open(dest, 'wb') as dst:
                    shutil.copyfileobj(src, dst)
        else:
            zf.extractall(version_dir)

    return version_dir


def preserve_existing_env(current_dir, version_dir):
    """Keep an existing .env when updating so secrets are not overwritten."""
    current_env = os.path.join(current_dir, '.env')
    version_env = os.path.join(version_dir, '.env')
    if os.path.exists(current_env) and os.path.exists(version_env):
        shutil.copy2(current_env, version_env)
        return True
    return False


def switch_current_version(app_dir, version):
    """Point current/ to versions/v<N> using a junction/symlink or copy fallback."""
    current_dir = os.path.join(app_dir, 'current')
    version_dir = os.path.join(app_dir, 'versions', f'v{version}')

    if not os.path.exists(version_dir):
        raise ValueError(f'Version directory not found: {version_dir}')

    # On Windows, symlinks may require privileges; fall back to a full copy.
    try:
        if os.path.islink(current_dir) or os.path.exists(current_dir):
            is_junction = (
                hasattr(os.path, 'isjunction') and os.path.isjunction(current_dir)
            ) if os.name == 'nt' else False
            if os.path.islink(current_dir) or is_junction:
                os.remove(current_dir)
            else:
                shutil.rmtree(current_dir)
        if os.name == 'nt':
            os.symlink(version_dir, current_dir, target_is_directory=True)
        else:
            os.symlink(version_dir, current_dir)
    except (OSError, NotImplementedError):
        if os.path.exists(current_dir):
            shutil.rmtree(current_dir)
        shutil.copytree(version_dir, current_dir)

    return current_dir


def backup_current(app_dir):
    """Backup current/ before overwriting. Returns the backup path or None."""
    current_dir = os.path.join(app_dir, 'current')
    if not os.path.exists(current_dir):
        return None
    backup_dir = os.path.join(app_dir, 'backups')
    os.makedirs(backup_dir, exist_ok=True)
    timestamp = datetime.utcnow().strftime('%Y%m%d-%H%M%S')
    backup_path = os.path.join(backup_dir, f'backup-{timestamp}')
    if os.path.exists(backup_path):
        shutil.rmtree(backup_path)
    shutil.copytree(current_dir, backup_path)
    return backup_path


def save_upload_zip(app_dir, zippath, version):
    """Persist the uploaded zip in uploads/."""
    uploads_dir = os.path.join(app_dir, 'uploads')
    os.makedirs(uploads_dir, exist_ok=True)
    dest = os.path.join(uploads_dir, f'v{version}.zip')
    shutil.copy2(zippath, dest)
    return dest


def list_versions(app_dir):
    """Return a sorted list of version dicts for an uploaded app."""
    versions_dir = os.path.join(app_dir, 'versions')
    if not os.path.exists(versions_dir):
        return []
    versions = []
    for entry in sorted(os.listdir(versions_dir)):
        if not entry.startswith('v'):
            continue
        try:
            num = int(entry[1:])
        except ValueError:
            continue
        path = os.path.join(versions_dir, entry)
        versions.append({
            'version': num,
            'path': path,
            'created_at': datetime.utcfromtimestamp(os.path.getctime(path)).isoformat(),
        })
    return versions


def get_current_version(app_dir):
    """Return the currently active version number or None."""
    current_dir = os.path.join(app_dir, 'current')
    if not os.path.exists(current_dir):
        return None
    try:
        target = os.path.realpath(current_dir)
        name = os.path.basename(target)
        if name.startswith('v'):
            return int(name[1:])
    except Exception:
        pass
    return None
