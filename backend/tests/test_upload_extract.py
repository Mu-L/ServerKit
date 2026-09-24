"""extract_version: what an uploaded archive becomes on disk."""
import zipfile

from app.services.upload_service import extract_version


def _zip(path, entries):
    with zipfile.ZipFile(path, 'w') as archive:
        for name, content in entries.items():
            archive.writestr(name, content)
    return str(path)


def test_single_file_archive_keeps_its_file(tmp_path):
    # Found by a deploy benchmark (2026-09-24): a zip
    # holding only docker-compose.yml extracted to an empty directory.
    archive = _zip(tmp_path / 'app.zip', {'docker-compose.yml': 'services: {}\n'})
    version_dir = extract_version(str(tmp_path / 'app'), archive, 1)
    assert (tmp_path / 'app' / 'versions' / 'v1' / 'docker-compose.yml').read_text() == 'services: {}\n'
    assert version_dir.endswith('v1')


def test_wrapper_folder_is_stripped(tmp_path):
    archive = _zip(tmp_path / 'app.zip', {'site/': '', 'site/index.html': 'hi', 'site/css/a.css': 'x'})
    extract_version(str(tmp_path / 'app'), archive, 1)
    root = tmp_path / 'app' / 'versions' / 'v1'
    assert (root / 'index.html').read_text() == 'hi'
    assert (root / 'css' / 'a.css').read_text() == 'x'


def test_flat_archive_extracts_as_is(tmp_path):
    archive = _zip(tmp_path / 'app.zip', {'index.html': 'hi', 'docker-compose.yml': 'services: {}\n'})
    extract_version(str(tmp_path / 'app'), archive, 1)
    root = tmp_path / 'app' / 'versions' / 'v1'
    assert sorted(p.name for p in root.iterdir()) == ['docker-compose.yml', 'index.html']
