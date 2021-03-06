#!/usr/bin/env python
"""Python utilities to simplify calls to some parts of the Data Manager API to
interact with **Projects**, **Instances** (**Jobs**) and **Files**.

.. note::
    The URL to the DM API is picked automatically up from the environment variable
    ``SQUONK_API_URL``, expected to be of the form **https://example.com/data-manager-api**.
    If the variable isn't set the user must set it programmatically
    using :py:meth:`DmApi.set_api_url()`.
"""
from collections import namedtuple
from datetime import datetime
import json
import logging
import os
from typing import Any, Dict, List, Optional, Tuple, Union
import urllib
from urllib3.exceptions import InsecureRequestWarning
from urllib3 import disable_warnings

from authlib.jose import jwt
from wrapt import synchronized
import requests

DmApiRv: namedtuple = namedtuple('DmApiRv', 'success msg')
"""The return value from most of the the DmApi class public methods.

:param success: True if the call was successful, False otherwise.
:param msg: API request response content
"""

TEST_PRODUCT_ID: str = 'product-11111111-1111-1111-1111-111111111111'
"""A test AS Product ID, This ID does not actually exist but is accepted
as valid by the Data Manager for Administrative users and used for
testing purposes.
"""

# The Job instance Application ID - a 'well known' identity.
_DM_JOB_APPLICATION_ID: str = 'datamanagerjobs.squonk.it'
# The API URL environment variable
_API_URL_ENV_NAME: str = 'SQUONK_API_URL'

# How old do tokens need to be to re-use them?
# If less than the value provided here, we get a new one.
# Used in get_access_token().
_PRIOR_TOKEN_MIN_AGE_M: int = 1

_LOGGER: logging.Logger = logging.getLogger(__name__)


class DmApi:
    """The DmAPI class provides high-level, simplified access to the DM API.
    You can use the request module directly for finer control. This module
    provides a wrapper around the handling of the request, returning a simplified
    namedtuple response value ``DmApiRv``
    """

    # The default DM API is extracted from the environment,
    # otherwise it can be set using 'set_api_url()'
    _dm_api_url: str = os.environ.get(_API_URL_ENV_NAME, '')
    # Do we expect the DM API to be secure?
    # This can be disabled using 'set_api_url()'
    _verify_ssl_cert: bool = True

    # The most recent access token Host and public key.
    # Set during token collection.
    _access_token_realm_url: str = ''
    _access_token_public_key: str = ''

    @classmethod
    def _request(cls,
                 method: str,
                 endpoint: str,
                 error_message: str,
                 access_token: Optional[str] = None,
                 expected_response_codes: Optional[List[int]] = None,
                 headers: Optional[Dict[str, Any]] = None,
                 data: Optional[Dict[str, Any]] = None,
                 files: Optional[Dict[str, Any]] = None,
                 params: Optional[Dict[str, Any]] = None,
                 timeout: int = 4)\
            -> Tuple[DmApiRv, Optional[requests.Response]]:
        """Sends a request to the DM API endpoint. The caller normally has to provide
        an oauth-like access token but this is not mandated. Some DM API methods
        use DM-generated tokens rather than access tokens. If so the caller will pass
        this through via the URL or 'params' - whatever is appropriate for the call.

        All the public API methods pass control to this method,
        returning its result to the user.
        """
        assert method in ['GET', 'POST', 'PUT', 'PATCH', 'DELETE']
        assert endpoint
        assert isinstance(expected_response_codes, (type(None), list))

        if not DmApi._dm_api_url:
            return DmApiRv(success=False,
                           msg={'error': 'No API URL defined'}), None

        url: str = DmApi._dm_api_url + endpoint

        # if we have it, add the access token to the headers,
        # or create a headers block
        use_headers = headers.copy() if headers else {}
        if access_token:
            if headers:
                use_headers['Authorization'] = 'Bearer ' + access_token
            else:
                use_headers = {'Authorization': 'Bearer ' + access_token}

        expected_codes = expected_response_codes if expected_response_codes else [200]
        resp: Optional[requests.Response] = None
        try:
            # Send the request (displaying the request/response)
            # and returning the response, whatever it is.
            resp = requests.request(method.upper(), url,
                                    headers=use_headers,
                                    params=params,
                                    data=data,
                                    files=files,
                                    timeout=timeout,
                                    verify=DmApi._verify_ssl_cert)
        except:
            _LOGGER.exception('Request failed')
        if resp is None or resp.status_code not in expected_codes:
            return DmApiRv(success=False,
                           msg={'error': f'{error_message} (resp={resp})'}),\
                   resp

        # Try and decode the response,
        # replacing with empty dictionary on failure.
        try:
            msg = resp.json()
        except:
            msg = {}
        return DmApiRv(success=True, msg=msg), resp

    @classmethod
    def _get_latest_job_operator_version(cls,
                                         access_token: str,
                                         timeout_s: int = 4)\
            -> Optional[str]:
        """Gets Job application data frm the DM API.
        We'll get and return the latest version found so that we can launch
        Jobs. If the Job application info is not available it indicates
        the server has no Job Operator installed.
        """
        assert access_token

        ret_val, resp = DmApi.\
            _request('GET',
                     f'/application/{_DM_JOB_APPLICATION_ID}',
                     access_token=access_token,
                     error_message='Failed getting Job application info',
                     timeout=timeout_s)
        if not ret_val.success:
            _LOGGER.error('Failed getting Job application info [%s]', resp)
            return None

        # If there are versions, return the first in the list
        assert resp is not None
        if 'versions' in resp.json() and len(resp.json()['versions']):
            return resp.json()['versions'][0]

        _LOGGER.warning('No versions returned for Job application info'
                        ' - no operator?')
        return ''

    @classmethod
    def _put_unmanaged_project_file(cls,
                                    access_token: str,
                                    project_id: str,
                                    project_file: str,
                                    project_path: str = '/',
                                    timeout_s: int = 120)\
            -> DmApiRv:
        """Puts an individual file into a DM project.
        """
        data: Dict[str, Any] = {}
        if project_path:
            data['path'] = project_path
        files = {'file': open(project_file, 'rb')}  # pylint: disable=consider-using-with

        ret_val, resp = DmApi.\
            _request('PUT', f'/project/{project_id}/file',
                     access_token=access_token,
                     data=data,
                     files=files,
                     expected_response_codes=[201],
                     error_message=f'Failed putting file {project_path}/{project_file}',
                     timeout=timeout_s)

        if not ret_val.success:
            _LOGGER.warning('Failed putting file %s -> %s (resp=%s project_id=%s)',
                            project_file, project_path, resp, project_id)
        return ret_val

    @classmethod
    @synchronized
    def set_api_url(cls, url: str, verify_ssl_cert: bool = True) -> None:
        """Replaces the API URL value, which is otherwise set using
        the ``SQUONK_API_URL`` environment variable.

        :param url: The API endpoint, typically **https://example.com/data-manager-api**
        :param verify_ssl_cert: Use False to avoid SSL verification in request calls
        """
        assert url
        DmApi._dm_api_url = url
        DmApi._verify_ssl_cert = verify_ssl_cert

        # Disable the 'InsecureRequestWarning'?
        if not verify_ssl_cert:
            disable_warnings(InsecureRequestWarning)

    @classmethod
    @synchronized
    def get_api_url(cls) -> Tuple[str, bool]:
        """Return the API URL and whether validating the SSL layer.
        """
        return DmApi._dm_api_url, DmApi._verify_ssl_cert

    @classmethod
    @synchronized
    def get_access_token(cls,
                         keycloak_url: str,
                         keycloak_realm: str,
                         keycloak_client_id: str,
                         username: str,
                         password: str,
                         prior_token: Optional[str] = None,
                         timeout_s: int = 4)\
            -> Optional[str]:
        """Gets a DM API access token from the given Keycloak server, realm
        and client ID.

        If keycloak fails to yield a token None is returned, with messages
        written to the log.

        The caller can (is encouraged to) provide a prior token in oprder to
        reduce token requests on the server. When a ``prior_token`` is provided
        the code only calls keycloak to obtain a new token if the current
        one looks like it will expire (in less than 60 seconds).

        :param keycloak_url: The keycloak server URL, typically **https://example.com/auth**
        :param keycloak_realm: The keycloak realm
        :param keycloak_client_id: The keycloak DM-API client ID
        :param username: A valid username
        :param password: A valid password
        :param prior_token: An optional prior token. If supplied it will be used
            unless it is about to expire
        :param timeout_s: The underlying request timeout
        """
        assert keycloak_url
        assert keycloak_realm
        assert keycloak_client_id
        assert username
        assert password

        # Do we have the public key for this host/realm?
        # if not grab it now.
        realm_url: str = f'{keycloak_url}/realms/{keycloak_realm}'
        if prior_token and DmApi._access_token_realm_url != realm_url:
            # New realm URL, remember and get the public key
            DmApi._access_token_realm_url = realm_url
            with urllib.request.urlopen(realm_url) as realm_stream:
                response = realm_stream.read()
                public_key = json.loads(response)['public_key']
            assert public_key
            key = '-----BEGIN PUBLIC KEY-----\n' +\
                  public_key +\
                  '\n-----END PUBLIC KEY-----'
            DmApi._access_token_public_key = key.encode('ascii')

        # If a prior token's been supplied,
        # re-use it if there's still time left before expiry.
        if prior_token:
            assert DmApi._access_token_public_key
            decoded_token: Dict[str, Any] = jwt.\
                decode(prior_token, DmApi._access_token_public_key)
            utc_timestamp: int = int(datetime.utcnow().timestamp())
            token_remaining_seconds: int = decoded_token['exp'] - utc_timestamp
            if token_remaining_seconds >= _PRIOR_TOKEN_MIN_AGE_M * 60:
                # Plenty of time left on the prior token,
                # return it to the user
                return prior_token

        # No prior token, or not enough time left on the one given.
        # Get a new token.
        data: str = f'client_id={keycloak_client_id}'\
            f'&grant_type=password'\
            f'&username={username}'\
            f'&password={password}'
        headers: Dict[str, Any] =\
            {'Content-Type': 'application/x-www-form-urlencoded'}
        url = f'{realm_url}/protocol/openid-connect/token'

        try:
            resp: requests.Response = requests.\
                post(url, headers=headers, data=data, timeout=timeout_s)
        except:
            _LOGGER.exception('Failed to get response from Keycloak')
            return None

        if resp.status_code not in [200]:
            _LOGGER.error('Failed to get token status_code=%s text=%s',
                          resp.status_code, resp.text)
            assert False

        assert 'access_token' in resp.json()
        return resp.json()['access_token']

    @classmethod
    @synchronized
    def ping(cls, access_token: str, timeout_s: int = 4)\
            -> DmApiRv:
        """A handy API method that calls the DM API to ensure the server is
        responding.

        :param access_token: A valid DM API access token
        :param timeout_s: The underlying request timeout
        """
        assert access_token

        return DmApi._request('GET', '/account-server/namespace',
                              access_token=access_token,
                              error_message='Failed ping',
                              timeout=timeout_s)[0]

    @classmethod
    @synchronized
    def get_version(cls, access_token: str, timeout_s: int = 4)\
            -> DmApiRv:
        """Returns the DM-API service version.

        :param access_token: A valid DM API access token
        :param timeout_s: The underlying request timeout
        """
        assert access_token

        return DmApi._request('GET', '/version',
                              access_token=access_token,
                              error_message='Failed getting version',
                              timeout=timeout_s)[0]

    @classmethod
    @synchronized
    def create_project(cls,
                       access_token: str,
                       project_name: str,
                       as_tier_product_id: str,
                       timeout_s: int = 4)\
            -> DmApiRv:
        """Creates a Project, which requires a name and a Product ID obtained from
        the Account Server.

        :param access_token: A valid DM API access token.
        :param project_name: A unique name.
        :param as_tier_product_id: If no account server is
            attached any suitable value can be used. If you are an admin user
            you can also use the reserved value of
            ``product-11111111-1111-1111-1111-111111111111``
            which is automatically accepted.
        :param timeout_s: The API request timeout
        """
        assert access_token
        assert project_name
        assert as_tier_product_id

        data: Dict[str, Any] = {'tier_product_id': as_tier_product_id,
                                'name': project_name}
        return DmApi._request('POST', '/project',
                              access_token=access_token,
                              data=data,
                              expected_response_codes=[201],
                              error_message='Failed creating project',
                              timeout=timeout_s)[0]

    @classmethod
    @synchronized
    def delete_project(cls,
                       access_token: str,
                       project_id: str,
                       timeout_s: int = 4) \
            -> DmApiRv:
        """Deletes a project.

        :param access_token: A valid DM API access token
        :param project_id: The DM-API project id to delete
        :param timeout_s: The underlying request timeout
        """
        assert access_token
        assert project_id

        return DmApi._request('DELETE', f'/project/{project_id}',
                              access_token=access_token,
                              error_message='Failed deleting project',
                              timeout=timeout_s)[0]

    @classmethod
    @synchronized
    def put_unmanaged_project_files(cls,
                                    access_token: str,
                                    project_id: str,
                                    project_files: Union[str, List[str]],
                                    project_path: str = '/',
                                    force: bool = False,
                                    timeout_per_file_s: int = 120)\
            -> DmApiRv:
        """Puts a file, or list of files, into a DM Project
        using an optional path.

        :param access_token: A valid DM API access token
        :param project_id: The project where the files are to be written
        :param project_files: A file or list of files. Leading paths are stripped
            so the two file files ``['dir/file-a.txt', 'file-b.txt']`` would
            be written to the same project directory, i.e. appearing as
            ``/file-a.txt`` and ``/file-b.txt`` in the project
        :param project_path: The path in the project to write the files.
            The path is relative to the project root and must begin ``/``
        :param force: Files are not written to the project if a file of the
            same name exists. Here ``force`` can be used to over-write files.
            Files on the server that are immutable cannot be over-written,
            and doing so will result in an error
        :param timeout_s: The underlying request timeout
        """

        assert access_token
        assert project_id
        assert project_files
        assert isinstance(project_files, (list, str))
        assert project_path\
               and isinstance(project_path, str)\
               and project_path.startswith('/')

        if not DmApi._dm_api_url:
            return DmApiRv(success=False, msg={'error': 'No API URL defined'})

        # If we're not forcing the files collect the names
        # of every file on the path - we use this to skip files that
        # are already present.
        existing_path_files: List[str] = []
        if force:
            _LOGGER.warning('Putting files (force=true project_id=%s)',
                            project_id)
        else:
            # What files already exist on the path?
            # To save time we avoid putting files that appear to exist.
            params: Dict[str, Any] = {'project_id': project_id}
            if project_path:
                params['path'] = project_path

            ret_val, resp = DmApi.\
                _request('GET', '/file', access_token=access_token,
                         expected_response_codes=[200, 404],
                         error_message='Failed getting existing project files',
                         params=params)
            if not ret_val.success:
                return ret_val

            assert resp is not None
            if resp.status_code in [200]:
                for item in resp.json()['files']:
                    existing_path_files.append(item['file_name'])

        # Now post every file that's not in the existing list
        if isinstance(project_files, str):
            src_files = [project_files]
        else:
            src_files = project_files
        for src_file in src_files:
            # Source file has to exist
            # whether we end up sending it or not.
            if not os.path.isfile(src_file):
                return DmApiRv(success=False,
                               msg={'error': f'No such file ({src_file})'})
            if os.path.basename(src_file) not in existing_path_files:
                ret_val = DmApi.\
                    _put_unmanaged_project_file(access_token,
                                                project_id,
                                                src_file,
                                                project_path,
                                                timeout_per_file_s)

                if not ret_val.success:
                    return ret_val

        # OK if we get here
        return DmApiRv(success=True, msg={})

    @classmethod
    @synchronized
    def delete_unmanaged_project_files(cls,
                                       access_token: str,
                                       project_id: str,
                                       project_files: Union[str, List[str]],
                                       project_path: str = '/',
                                       timeout_s: int = 4)\
            -> DmApiRv:
        """Deletes an unmanaged project file, or list of files, on a project path.

        :param access_token: A valid DM API access token
        :param project_id: The project where the files are present
        :param project_files: A file or list of files. Leading paths are stripped
            so the two file files ``['dir/file-a.txt', 'file-b.txt']`` would
            be expected to be in the same project directory, i.e. appearing as
            ``/file-a.txt`` and ``/file-b.txt`` in the project
        :param project_path: The path in the project where the files are located.
            The path is relative to the project root and must begin ``/``
        :param timeout_s: The underlying request timeout
        """
        assert access_token
        assert project_id
        assert isinstance(project_files, (list, str))
        assert project_path\
               and isinstance(project_path, str)\
               and project_path.startswith('/')

        if isinstance(project_files, str):
            files_to_delete = [project_files]
        else:
            files_to_delete = project_files

        for file_to_delete in files_to_delete:
            params: Dict[str, Any] = {'project_id': project_id,
                                      'path': project_path,
                                      'file': file_to_delete}
            ret_val, _ =\
                DmApi._request('DELETE', '/file',
                               access_token=access_token,
                               params=params,
                               expected_response_codes=[204],
                               error_message='Failed to delete project file',
                               timeout=timeout_s)
            if not ret_val.success:
                return ret_val

        # OK if we get here
        return DmApiRv(success=True, msg={})

    @classmethod
    @synchronized
    def list_project_files(cls,
                           access_token: str,
                           project_id: str,
                           project_path: str = '/',
                           include_hidden: bool = False,
                           timeout_s: int = 8)\
            -> DmApiRv:
        """Gets a list of project files on a path.

        :param access_token: A valid DM API access token
        :param project_id: The project where the files are present
        :param project_path: The path in the project to search for files.
            The path is relative to the project root and must begin ``/``
        :param timeout_s: The underlying request timeout
        """
        assert access_token
        assert project_id
        assert project_path\
               and isinstance(project_path, str)\
               and project_path.startswith('/')

        params: Dict[str, Any] = {'project_id': project_id,
                                  'path': project_path,
                                  'include_hidden': include_hidden}
        return DmApi._request('GET', '/file',
                              access_token=access_token,
                              params=params,
                              error_message='Failed to list project files',
                              timeout=timeout_s)[0]

    @classmethod
    @synchronized
    def get_unmanaged_project_file(cls,
                                   access_token: str,
                                   project_id: str,
                                   project_file: str,
                                   local_file: str,
                                   project_path: str = '/',
                                   timeout_s: int = 8)\
            -> DmApiRv:
        """Get a single unmanaged file from a project path, saving it to
        the filename defined in local_file.

        :param access_token: A valid DM API access token
        :param project_id: The project where the files are present
        :param project_file: The name of the file to get
        :param local_file: The name to use to write the file to on the client
        :param project_path: The path in the project to search for files.
            The path is relative to the project root and must begin ``/``
        :param timeout_s: The underlying request timeout
        """
        assert access_token
        assert project_id
        assert project_file
        assert local_file
        assert project_path\
               and isinstance(project_path, str)\
               and project_path.startswith('/')

        params: Dict[str, Any] = {'path': project_path,
                                  'file': project_file}
        ret_val, resp = DmApi._request('GET', f'/project/{project_id}/file',
                                       access_token=access_token,
                                       params=params,
                                       error_message='Failed to get file',
                                       timeout=timeout_s)
        if not ret_val.success:
            return ret_val

        # OK if we get here
        assert resp is not None
        with open(local_file, 'wb') as file_handle:
            file_handle.write(resp.content)
        return ret_val

    @classmethod
    @synchronized
    def get_unmanaged_project_file_with_token(cls,
                                              token: str,
                                              project_id: str,
                                              project_file: str,
                                              local_file: str,
                                              project_path: str = '/',
                                              timeout_s: int = 8)\
            -> DmApiRv:
        """Like :py:meth:`~DmApi.get_unmanaged_project_file()`, this method
        get a single unmanaged file from a project path. The method uses an
        Instance-generated callback token rathgr than a user-access token.

        This method is particularly useful in callback routines where a user
        access token may not be available. Callback tokens expire and can be
        deleted and so this function should only be used when a user access
        token is not available.

        :param token: A DM-generated token, optionally generated when
            launching instances in the project
        :param project_id: The project where the files are present
        :param project_file: The name of the file to get
        :param local_file: The name to use to write the file to on the client
        :param project_path: The path in the project to search for files.
            The path is relative to the project root and must begin ``/``
        :param timeout_s: The underlying request timeout
        """
        assert token
        assert project_id
        assert project_file
        assert local_file
        assert project_path\
               and isinstance(project_path, str)\
               and project_path.startswith('/')

        params: Dict[str, Any] = {'path': project_path,
                                  'file': project_file,
                                  'token': token}
        ret_val, resp = DmApi._request('GET', f'/project/{project_id}/file-with-token',
                                       params=params,
                                       error_message='Failed to get file',
                                       timeout=timeout_s)
        if not ret_val.success:
            return ret_val

        # OK if we get here
        assert resp is not None
        with open(local_file, 'wb') as file_handle:
            file_handle.write(resp.content)
        return ret_val

    @classmethod
    @synchronized
    def start_job_instance(cls,
                           access_token: str,
                           project_id: str,
                           name: str,
                           specification: Dict[str, Any],
                           callback_url: Optional[str] = None,
                           callback_context: Optional[str] = None,
                           generate_callback_token: bool = False,
                           debug: Optional[str] = None,
                           timeout_s: int = 4)\
            -> DmApiRv:
        """Instantiates a Job Instance in a Project.

        :param access_token: A valid DM API access token
        :param project_id: The project where the files are present
        :param name: A name to associate with the Job
        :param specification: The Job specification, it must contain
            keys that define the Job's ``collection``, ``job name`` and
            ``version``. Job-specific variables are passed in using a ``variables``
            map in the specification
        :param callback_url: An optional URL capable of handling Job callbacks.
            Must be set if ``generate_callback_token`` is used
        :param callback_context: An optional context string passed to the
            callback URL
        :param generate_callback_token: True to instruct the DM to generate
            a token that can be used with some methods instead of a
            user access token
        :param debug: Used to prevent the automatic removal of the Job instance.
            Only use this if you need to
        :param timeout_s: The underlying request timeout
        """

        assert access_token
        assert project_id
        assert name
        assert isinstance(specification, (type(None), dict))

        # Get the latest Job operator version.
        # If there isn't one the DM can't run Jobs.
        job_application_version: Optional[str] =\
            DmApi._get_latest_job_operator_version(access_token)
        if job_application_version is None:
            # Failed calling the server.
            # Incorrect URL, bad token or server out of action?
            return DmApiRv(success=False,
                           msg={'error': 'Failed getting Job operator version'})
        if not job_application_version:
            return DmApiRv(success=False,
                           msg={'error': 'No Job operator installed'})

        data: Dict[str, Any] =\
            {'application_id': _DM_JOB_APPLICATION_ID,
             'application_version': job_application_version,
             'as_name': name,
             'project_id': project_id,
             'specification': json.dumps(specification)}
        if debug:
            data['debug'] = debug
        if callback_url:
            data['callback_url'] = callback_url
            if callback_context:
                data['callback_context'] = callback_context
            if generate_callback_token:
                data['generate_callback_token'] = True

        return DmApi._request('POST', '/instance', access_token=access_token,
                              expected_response_codes=[201],
                              error_message='Failed to start instance',
                              data=data, timeout=timeout_s)[0]

    @classmethod
    @synchronized
    def get_available_projects(cls, access_token: str, timeout_s: int = 4)\
            -> DmApiRv:
        """Gets information about all projects available to you.

        :param access_token: A valid DM API access token
        :param timeout_s: The underlying request timeout
        """
        assert access_token

        return DmApi._request('GET', '/project',
                              access_token=access_token,
                              error_message='Failed to get projects',
                              timeout=timeout_s)[0]

    @classmethod
    @synchronized
    def get_project(cls,
                    access_token: str,
                    project_id: str,
                    timeout_s: int = 4)\
            -> DmApiRv:
        """Gets detailed information about a specific project.

        :param access_token: A valid DM API access token
        :param project_id: The specific project to retrieve
        :param timeout_s: The underlying request timeout
        """
        assert access_token
        assert project_id

        return DmApi._request('GET', f'/project/{project_id}',
                              access_token=access_token,
                              error_message='Failed to get project',
                              timeout=timeout_s)[0]

    @classmethod
    @synchronized
    def get_instance(cls,
                     access_token: str,
                     instance_id: str,
                     timeout_s: int = 4)\
            -> DmApiRv:
        """Gets information about an instance (Application or Job).

        :param access_token: A valid DM API access token
        :param instance_id: The specific instance to retrieve
        :param timeout_s: The underlying request timeout
        """
        assert access_token
        assert instance_id

        return DmApi._request('GET', f'/instance/{instance_id}',
                              access_token=access_token,
                              error_message='Failed to get instance',
                              timeout=timeout_s)[0]

    @classmethod
    @synchronized
    def get_project_instances(cls,
                              access_token: str,
                              project_id: str,
                              timeout_s: int = 4)\
            -> DmApiRv:
        """Gets information about all instances available to you.

        :param access_token: A valid DM API access token
        :param timeout_s: The underlying request timeout
        """
        assert access_token
        assert project_id

        params: Dict[str, Any] = {'project_id': project_id}
        return DmApi._request('GET', '/instance',
                              access_token=access_token,
                              params=params,
                              error_message='Failed to get project instances',
                              timeout=timeout_s)[0]

    @classmethod
    @synchronized
    def delete_instance(cls,
                        access_token: str,
                        instance_id: str,
                        timeout_s: int = 4)\
            -> DmApiRv:
        """Deletes an Instance (Application or Job).

        When instances are deleted the container is removed along with
        the instance-specific directory that is automatically created
        in the root of the project. Any files in the instance-specific
        directory wil be removed.

        :param access_token: A valid DM API access token
        :param instance_id: The instance to delete
        :param timeout_s: The underlying request timeout
        """
        assert access_token
        assert instance_id

        return DmApi._request('DELETE', f'/instance/{instance_id}',
                              access_token=access_token,
                              error_message='Failed to delete instance',
                              timeout=timeout_s)[0]

    @classmethod
    @synchronized
    def delete_instance_token(cls,
                              instance_id: str,
                              token: str,
                              timeout_s: int = 4)\
            -> DmApiRv:
        """Deletes a DM API Instance **callback token**. This API method is not
        authenticated and therefore does not need an access token. Once the token is
        deleted no further calls to :py:meth:`DmApi.get_unmanaged_project_file_with_token()`
        will be possible. Once deleted the token cannot be re-instantiated.

        :param instance_id: A valid DM API instance
        :param token: The callback Token associated with the instance
        :param timeout_s: The API request timeout
        """
        assert instance_id
        assert token

        return DmApi._request('DELETE', f'/instance/{instance_id}/token/{token}',
                              error_message='Failed to delete instance token',
                              timeout=timeout_s)[0]

    @classmethod
    @synchronized
    def get_task(cls,
                 access_token: str,
                 task_id: str,
                 event_prior_ordinal: int = 0,
                 event_limit: int = 0,
                 timeout_s: int = 4)\
            -> DmApiRv:
        """Gets information about a specific Task
        """
        assert access_token
        assert task_id
        assert event_prior_ordinal >= 0
        assert event_limit >= 0

        params: Dict[str, Any] = {}
        if event_prior_ordinal:
            params['event_prior_ordinal'] = event_prior_ordinal
        if event_limit:
            params['event_limit'] = event_limit
        return DmApi._request('GET', f'/task/{task_id}',
                              access_token=access_token,
                              params=params,
                              error_message='Failed to get task',
                              timeout=timeout_s)[0]

    @classmethod
    @synchronized
    def get_available_jobs(cls, access_token: str, timeout_s: int = 4)\
            -> DmApiRv:
        """Gets a summary list of available Jobs.

        :param access_token: A valid DM API access token.
        :param timeout_s: The API request timeout
        """
        assert access_token

        return DmApi._request('GET', '/job',
                              access_token=access_token,
                              error_message='Failed to get available jobs',
                              timeout=timeout_s)[0]

    @classmethod
    @synchronized
    def get_job(cls, access_token: str, job_id: int, timeout_s: int = 4)\
            -> DmApiRv:
        """Gets detailed information about a specific Job
        using the numeric Job record identity

        :param access_token: A valid DM API access token.
        :param job_id: The numeric Job identity
        :param timeout_s: The API request timeout
        """
        assert access_token
        assert job_id > 0

        return DmApi._request('GET', f'/job/{job_id}',
                              access_token=access_token,
                              error_message='Failed to get job',
                              timeout=timeout_s)[0]

    @classmethod
    @synchronized
    def get_job_by_name(cls,
                        access_token: str,
                        job_collection: str,
                        job_name: str,
                        job_version: str,
                        timeout_s: int = 4)\
            -> DmApiRv:
        """Gets detailed information about a specific Job
        using the ``collection``, ``name`` and ``version``

        :param access_token: A valid DM API access token.
        :param job_collection: The Job collection, e.g. ``im-test``
        :param job_name: The Job name, e.g. ``nop``
        :param job_version: The Job version, e.g. ``1.0.0``
        :param timeout_s: The API request timeout
        """
        assert access_token
        assert job_collection
        assert job_name
        assert job_version

        params: Dict[str, Any] = {'collection': job_collection,
                                  'name': job_name,
                                  'version': job_version}
        return DmApi._request('GET', '/job/get-by-name',
                              access_token=access_token,
                              params=params,
                              error_message='Failed to get job',
                              timeout=timeout_s)[0]

    @classmethod
    @synchronized
    def set_admin_state(cls,
                        access_token: str,
                        admin: bool,
                        impersonate: Optional[str] = None,
                        timeout_s: int = 4)\
            -> DmApiRv:
        """Adds or removes the ``become-admin`` state of your account.
        Only users whose accounts offer administrative capabilities
        can use this method.

        :param access_token: A valid DM API access token.
        :param admin: True to set admin state
        :param imperrsonate: An optional username to switch to
        :param timeout_s: The API request timeout
        """
        assert access_token

        data: Dict[str, Any] = {'become_admin': admin}
        if impersonate:
            data['impersonate'] = impersonate

        return DmApi._request('PATCH', '/user/account',
                              access_token=access_token,
                              data=data,
                              expected_response_codes=[204],
                              error_message='Failed to set the admin state',
                              timeout=timeout_s)[0]
