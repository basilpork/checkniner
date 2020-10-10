import datetime
import hashlib
import logging
import os
import tarfile

from azure.storage.blob import BlockBlobService
from boto.s3.connection import S3Connection
from boto.s3.bucket import Bucket
from boto.s3.key import Key
import dj_database_url
import django  # Provides django.setup()
from django.apps import apps as django_apps
import envoy


# ISO 8601 YYYY-MM-DDTHH:MM:SS
DATE_FORMAT = "%Y-%m-%dT%H:%M:%S"


# Would be '[]' without the '--indent=4' argument to dumpdata
EMPTY_FIXTURE = '[\n]\n'


logging.basicConfig(
    filename='backup.log',
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s]: %(message)s",
    datefmt=DATE_FORMAT,
)
logger = logging.getLogger(__name__)


def instrument(function):
    def wrapper(*args, **kwargs):
        start = datetime.datetime.now()
        r = function(*args, **kwargs)
        end = datetime.datetime.now() - start
        milliseconds = end.microseconds / 1000
        return (milliseconds, r)
    return wrapper


def capture_command(function):
    def wrapper(*args, **kwargs):
        if args:
            command = args[0]
        elif 'command' in kwargs:
            command = kwargs['command']
        else:
            logger.warning(
                "What's the command?.\n\targs: %r\n\tkwargs: %r" % (args, kwargs))
            command = '?'

        # Being much too clever: Dynamically decorate the given function with
        # the 'instrument' function so that we can capture timing data. Once
        # the function is decorated, we'll call it with the original
        # parameters.
        milliseconds, r = instrument(function)(*args, **kwargs)
        bytes = len(r.std_out)
        logger.info("cmd=\"%s\" real=%dms bytes=%d" %
                    (command, milliseconds, bytes))
        if hasattr(r, 'std_err'):
            # Print each non-blank line separately
            lines = [line for line in r.std_err.split('\n') if line]
            map(logger.error, lines)
        return r
    return wrapper


def capture_function(function):
    def wrapper(*args, **kwargs):
        milliseconds, r = instrument(function)(*args, **kwargs)
        logger.info("func=\"%s\" real=%dms" %
                    (function.__name__, milliseconds))
        return r
    return wrapper


# Keep a log of the calls through envoy
envoy.run = capture_command(envoy.run)


def get_database_name(env='DATABASE_URL'):
    db_config = dj_database_url.config(env)
    return db_config['NAME']


def get_installed_app_names():
    django.setup()
    apps = django_apps.get_app_configs()
    # Labels default to the last component of the app name, but not guaranteed:
    # - Default: 'django.contrib.auth' -> 'auth'
    # - Changed: 'raven.contrib.django.raven_compat' -> 'raven_contrib_django'
    labels = [appconfig.label for appconfig in apps]
    return labels


def get_s3_credentials():
    access = os.environ['S3_ACCESS_KEY']
    secret = os.environ['S3_SECRET_KEY']
    return (access, secret)


def get_s3_bucket_name():
    return os.environ['S3_BUCKET_NAME']


def get_az_credentials():
    account_name = os.environ['AZ_STORAGE_ACCOUNT_NAME']
    account_key = os.environ['AZ_STORAGE_ACCOUNT_KEY']
    return (account_name, account_key)


def get_az_container_name():
    return os.environ['AZ_BLOB_CONTAINER_NAME']


def dump_postgres():
    database = get_database_name()
    r = envoy.run("pg_dump %s" % database)
    filename = "%s.sql" % database
    with open(filename, 'w') as f:
        f.write(r.std_out)
    return filename


def dump_django_fixtures():
    filenames = []
    for name in get_installed_app_names():
        r = envoy.run('django-admin.py dumpdata %s --indent=4' % name)
        if r.std_out != EMPTY_FIXTURE:
            filename = "%s.json" % name
            with open(filename, 'w') as f:
                f.write(r.std_out)
            filenames.append(filename)
        else:
            logger.warning("Skipping empty fixture for '%s'" % name)
    return filenames


def sha1sum(file):
    m = hashlib.sha1()
    try:
        logger.debug("Treating '%s' as an open file" % file)
        m.update(file.read())
    except AttributeError:
        logger.debug("Now trying '%s' as a file path" % file)
        with open(file, 'rb') as f:
            m.update(f.read())
    return m.hexdigest()


@capture_function
def update_necessary():
    latest = 'latest.tar.gz'
    # Shortcut if we have nothing to compare against (e.g. the first run)
    if not os.path.exists(latest):
        logger.debug("Could not access '%s', assuming first run" % latest)
        return True
    to_compare = ('auth.json', 'checkouts.json')
    for name in to_compare:
        proposed = sha1sum(name)
        with tarfile.open(latest, 'r:gz') as tar:
            for tarinfo in tar:
                if tarinfo.name == name:
                    original = sha1sum(tar.extractfile(tarinfo))
                    logger.debug("Original: %s" % original)
                    logger.debug("Proposed: %s" % proposed)
                    if proposed != original:
                        logger.debug("Digests differ for '%s'" % name)
                        return True
    return False


@capture_function
def package(filenames):
    latest = 'latest.tar.gz'
    logger.info("Packaging archive")
    logger.debug("Contents: %s" % filenames)
    date = datetime.datetime.utcnow().strftime(DATE_FORMAT)
    filename = "%s.tar.gz" % date
    with tarfile.open(filename, 'w:gz') as archive:
        [archive.add(name) for name in filenames]
    if os.path.lexists(latest):
        os.unlink(latest)
    os.symlink(filename, latest)
    return filename


@capture_function
def encrypt(filename):
    encrypted_filename = '%s.gpg' % filename
    if os.path.isfile(encrypted_filename):
        os.path.remove(encrypted_filename)
    r = envoy.run("./encrypt.sh %s" % filename)
    return encrypted_filename


@capture_function
def az_upload(filename):
    if not filename.endswith('.gpg'):
        logger.warning(
            "Upload requested for '%s' which appears to be plaintext (not encrypted)" % filename)
    account_name, account_key = get_az_credentials()
    container_name = get_az_container_name()
    local_path = os.path.abspath(os.path.dirname(__file__))
    full_path_to_file = os.path.join(local_path, filename)
    block_blob_service = BlockBlobService(account_name, account_key)
    block_blob_service.create_blob_from_path(
        container_name, filename, full_path_to_file)
    logger.info("Uploaded '%s' to '%s:%s'" %
                (filename, account_name, container_name))
    return os.path.getsize(filename)


@capture_function
def s3_upload(filename):
    if not filename.endswith('.gpg'):
        logger.warning(
            "Upload requested for '%s' which appears to be plaintext (not encrypted)" % filename)
    access, secret = get_s3_credentials()
    bucket_name = get_s3_bucket_name()
    key_name = 'db/%s' % filename
    connection = S3Connection(access, secret)
    bucket = Bucket(connection, bucket_name)
    key = Key(bucket)
    key.key = key_name
    key.set_contents_from_filename(filename)
    logger.info("Uploaded '%s' to '%s:%s'" % (filename, bucket_name, key_name))
    return os.path.getsize(filename)


filenames = [dump_postgres(), ]
filenames.extend(dump_django_fixtures())
if update_necessary():
    archive = package(filenames)
    secured = encrypt(archive)
    filesize = az_upload(secured)
    logger.info(
        "---- Uploading complete ---- bytes=%d --------------------" % filesize)
else:
    logger.info("---- Digests match, ceasing activity ---------------------")
map(os.remove, filenames)
