import base64
import hashlib
import logging
import os.path
import rfc822
import sys
import tempfile

import boto3
import json
import marshmallow.fields
import requests
from requests_hawk import HawkAuth
from signing_clients.apps import JarExtractor
from six.moves.urllib.parse import urljoin

CHUNK_SIZE = 512 * 1024

logger = logging.getLogger()
logger.setLevel(logging.INFO)

s3 = boto3.resource('s3')

class SignXPIError(Exception):
    """Abstract base class for errors in this lambda."""


class ChecksumMatchError(SignXPIError):
    def __init__(self, url, expected_checksum, actual_checksum):
        message = "When fetching {}, expected checksum {} (got {})".format(
            url, expected_checksum, actual_checksum)
        super(ChecksumMatchError, self).__init__(message)
        self.url = url
        self.expected_checksum = expected_checksum
        self.actual_checksum = actual_checksum


class AutographInfo(marshmallow.Schema):
    hawk_id = marshmallow.fields.String(required=True, load_from="hawkId")
    hawk_secret = marshmallow.fields.String(required=True, load_from="hawkSecret")
    server_url = marshmallow.fields.String(required=True, load_from="serverUrl")
    key_id = marshmallow.fields.String(required=True, load_from="keyId")


class Context(marshmallow.Schema):
    autograph = marshmallow.fields.Nested(AutographInfo, required=True)
    output_bucket = marshmallow.fields.String(required=True, load_from="outputBucket")

class SourceInfo(marshmallow.Schema):
    """
    Describes the location of the XPI, as either a URL or an S3 location.

    Requires either URL, or bucket + key.
    """
    url = marshmallow.fields.URL()
    bucket = marshmallow.fields.String()
    key = marshmallow.fields.String()

    @marshmallow.decorators.validates_schema
    def verify_either_url_or_s3_info(self, data):
        if data.get('url'):
            return

        if data.get('bucket') and data.get('key'):
            return

        raise marshmallow.exceptions.ValidationError(
            "Either a URL or an S3 location (bucket + key) must be provided",
            ["url", "bucket", "key"])


class SignEvent(marshmallow.Schema):
    source = marshmallow.fields.Nested(SourceInfo, required=True)
    checksum = marshmallow.fields.String()


def handle(event, context):
    """
    Handle a sign-xpi event.
    """

    event = SignEvent(strict=True).load(event).data
    context = Context(strict=True).load(context).data

    (localfile, filename) = retrieve_xpi(event)
    guid = get_guid(localfile)
    signed_xpi = sign_xpi(context['autograph'], localfile, guid)
    return upload(context, file(signed_xpi), filename)


def upload(context, signed_xpi, filename):
    bucket = s3.Bucket(context['output_bucket'])
    bucket.put_object(Body=signed_xpi, Key=filename)

    return {
        "uploaded": {
            "bucket": bucket.name,
            "key": filename,
        }
    }


def retrieve_xpi(event):
    """
    Download the XPI to some local file, verifying that its checksum is correct.

    Returns a local "temporary" file containing the XPI as well as its "filename" as best as we could deduce.

    :return: (localfile, filename)
    """
    localfile = tempfile.NamedTemporaryFile()
    source = event['source']
    if source.get('url'):
        url = source['url']
        response = requests.get(url, stream=True)
        response.raise_for_status()

        (_, filename) = url.strip('/').rsplit('/', 1)
        filename = extract_response_filename(response) or filename
        for chunk in response.iter_content(chunk_size=CHUNK_SIZE):
            localfile.write(chunk)
    else:
        bucket = s3.Bucket(source['bucket'])
        key = source['key']
        (_, filename) = key.rsplit('/', 1)
        bucket.download_fileobj(key, localfile)

    localfile.seek(0)
    local_checksum = compute_checksum(localfile)
    if local_checksum != event['checksum']:
        raise ChecksumMatchError(url, event['checksum'], local_checksum)

    localfile.seek(0)
    return localfile, filename


def extract_response_filename(response):
    """Extract the content-disposition filename, or None if we can't."""
    content_disposition = response.headers.get('Content-Disposition')
    if not content_disposition:
        return None

    parameters = content_disposition.split(';')
    if parameters[0].strip() != 'attachment':
        return None

    for parameter in parameters[1:]:
        name, value = parameter.strip().split('=', 1)
        if name == 'filename':
            return rfc822.unquote(value)

    return None


def compute_checksum(contents):
    # Always use sha256 for now
    h = hashlib.sha256()
    h.update(contents.read())
    return h.hexdigest()


def get_guid(xpi_file):
    return "some-fake-guid@example.com"


def sign_xpi(autograph_info, localfile, guid):
    """
    Use the Autograph service to sign the XPI.

    :returns: filename of the signed XPI
    """
    jar_extractor = JarExtractor(localfile, extra_newlines=True)
    auth = HawkAuth(id=autograph_info['hawk_id'], key=autograph_info['hawk_secret'])
    b64_payload = base64.b64encode(jar_extractor.signature)
    url = urljoin(autograph_info['server_url'], '/sign/data')
    key_id = autograph_info['key_id']
    resp = requests.post(url, auth=auth, json=[{
        # FIXME: not Python 3 safe, but Amazon Lambda only supports
        # Python 2.7 anyhow so whatever
        "input": b64_payload,
        "keyid": key_id,
        "options": {
            "id": guid,
        }
    }])
    resp.raise_for_status()
    signature = base64.b64decode(resp.json()[0]['signature'])

    # FIXME: make_signed doesn't support NamedTemporaryFile or
    # anything like that; we have to provide an actual filename.
    # Try to generate a sensible filename.
    # FIXME: I guess the caller has to remember to delete this file.
    # Hopefully there's no other calls to a similarly-named file??
    (localstem, localext) = os.path.splitext(localfile.name)
    output_file = localstem + '-signed' + localext

    jar_extractor.make_signed(signature, output_file, sigpath="mozilla.rsa")
    return output_file

if __name__ == '__main__':
    AUTOGRAPH_INFO = {
        "serverUrl": "http://localhost:8000/",
        "hawkId": "alice",
        "hawkSecret": "fs5wgcer9qj819kfptdlp8gm227ewxnzvsuj9ztycsx08hfhzu",
        "keyId": "extensions-ecdsa",
    }
    context = {
        "autograph": AUTOGRAPH_INFO,
        "outputBucket": "eglassercamp-addon-sign-xpi-output",
    }
    print handle(json.loads(sys.stdin.read()), context)
