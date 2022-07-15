# Copyright Notice:
# Copyright 2016-2020 DMTF. All rights reserved.
# License: BSD 3-Clause License. For full text see link: https://github.com/DMTF/Redfish-Service-Validator/blob/master/LICENSE.md

import json
from datetime import datetime
from functools import lru_cache
from urllib.parse import urlparse, urlunparse
from http.client import responses
import os

import redfish as rf
import requests
import common.catalog as catalog
from common.helper import navigateJsonFragment, splitVersionString
from common.metadata import Metadata

import logging
my_logger = logging.getLogger(__name__)
traverseLogger = my_logger

# dictionary to hold sampling notation strings for URIs
class AuthenticationError(Exception):
    """Exception used for failed basic auth or token auth"""
    def __init__(self, msg=None):
        super(AuthenticationError, self).__init__(msg)

def getLogger():
    """
    Grab logger for tools that might use this lib
    """
    return my_logger

class rfService():
    def __init__(self, config):
        traverseLogger.info('Setting up service...')
        self.active, self.config = False, config
        self.logger = getLogger()

        self.config['configuri'] = self.config['ip']
        self.config['metadatafilepath'] = self.config['schema_directory']
        self.config['usessl'] = urlparse(self.config['configuri']).scheme in ['https']
        self.config['certificatecheck'] = False
        self.config['certificatebundle'] = None
        self.config['timeout'] = 10

        # Log into the service
        if not self.config['usessl'] and not self.config['forceauth']:
            if self.config['username'] not in ['', None] or self.config['password'] not in ['', None]:
                traverseLogger.warning('Attempting to authenticate on unchecked http/https protocol is insecure, if necessary please use ForceAuth option.  Clearing auth credentials...')
                self.config['username'] = ''
                self.config['password'] = ''
        rhost, user, passwd = self.config['configuri'], self.config['username'], self.config['password']
        proxies=None
        if self.config['serv_http_proxy'] != '' or self.config['serv_https_proxy'] != '':
            proxies = {}
            if self.config['serv_http_proxy'] != '': proxies['http'] = self.config['serv_http_proxy']
            if self.config['serv_https_proxy'] != '': proxies['https'] = self.config['serv_https_proxy']
        self.ext_proxies=None
        if self.config['ext_http_proxy'] != '' or self.config['ext_https_proxy'] != '':
            self.ext_proxies = {}
            if self.config['ext_http_proxy'] != '': self.ext_proxies['http'] = self.config['ext_http_proxy']
            if self.config['ext_https_proxy'] != '': self.ext_proxies['https'] = self.config['ext_https_proxy']
        self.context = rf.redfish_client(base_url=rhost, username=user, password=passwd, timeout=self.config['timeout'], proxies=proxies)
        self.context.login( auth = self.config['authtype'].lower() )

        # Go through $metadata and download any additional schema files needed
        success, data, response, delay = self.callResourceURI(Metadata.metadata_uri)
        if success and data is not None and response.status in range(200,210):
            self.metadata = Metadata(data, self, my_logger)
        else:
            self.metadata = Metadata(None, self, my_logger)

        # Build the data model based on cached schema files
        self.catalog = catalog.SchemaCatalog(self.config['metadatafilepath'])

        target_version = 'n/a'

        # get Version
        success, data, response, delay = self.callResourceURI('/redfish/v1')
        if not success:
            traverseLogger.warning('Could not get ServiceRoot')
        else:
            if 'RedfishVersion' not in data:
                traverseLogger.warning('Could not get RedfishVersion from ServiceRoot')
            else:
                traverseLogger.info('Redfish Version of Service: {}'.format(data['RedfishVersion']))
                target_version = data['RedfishVersion']
        if target_version in ['1.0.0', 'n/a']:
            traverseLogger.warning('!!Version of target may produce issues!!')
        if splitVersionString(target_version) < splitVersionString('1.6.0') and not self.config['uricheck']:
            traverseLogger.warning('RedfishVersion below 1.6.0, disabling uri checks')
            self.catalog.flags['ignore_uri_checks'] = True
        else:
            self.catalog.flags['ignore_uri_checks'] = False
            self.config['uricheck'] = True
        
        self.service_root = data

        self.active = True


    def close(self):
        self.active = False

    @lru_cache(maxsize=128)
    def callResourceURI(self, URILink):
        traverseLogger = my_logger
        """
        Makes a call to a given URI or URL

        param arg1: path to URI "/example/1", or URL "http://example.com"
        return: (success boolean, data, request status code)
        """
        # rs-assertions: 6.4.1, including accept, content-type and odata-versions
        # rs-assertion: handle redirects?  and target permissions
        # rs-assertion: require no auth for serviceroot calls
        # TODO: Written with "success" values, should replace with Exception and catches
        if URILink is None:
            traverseLogger.warning("This URI is empty!")
            return False, None, None, 0

        config = self.config
        # proxies = self.proxies
        ConfigIP, UseSSL, AuthType, ChkCert, ChkCertBundle, timeout, Token = config['configuri'], config['usessl'], config['authtype'], \
                config['certificatecheck'], config['certificatebundle'], config['timeout'], config['token']

        scheme, netloc, path, params, query, fragment = urlparse(URILink)
        inService = scheme == '' and netloc == ''
        if inService:
            URLDest = urlunparse((scheme, netloc, path, '', '', '')) #URILink
        else:
            URLDest = urlunparse((scheme, netloc, path, params, query, fragment))

        payload, statusCode, elapsed, auth, noauthchk = None, '', 0, None, True

        isXML = False
        if "$metadata" in path or ".xml" in path[:-5]:
            isXML = True
            traverseLogger.debug('Should be XML')

        # determine if we need to Auth...
        if inService:
            noauthchk =  URILink in ['/redfish', '/redfish/v1', '/redfish/v1/odata'] or\
                '/redfish/v1/$metadata' in URILink

            auth = None if noauthchk else (config.get('username'), config.get('password'))
            traverseLogger.debug('dont chkauth' if noauthchk else 'chkauth')

        # rs-assertion: do not send auth over http
        # remove UseSSL if necessary if you require unsecure auth
        if (not UseSSL and not config['forceauth']) or not inService or AuthType != 'Basic':
            auth = None

        # only send token when we're required to chkauth, during a Session, and on Service and Secure
        headers = {"Accept-Encoding": "*"}

        certVal = ChkCertBundle if ChkCert and ChkCertBundle not in [None, ""] else ChkCert

        # rs-assertion: must have application/json or application/xml
        traverseLogger.debug('callingResourceURI {}with authtype {} and ssl {}: {} {}'.format(
            'out of service ' if not inService else '', AuthType, UseSSL, URILink, headers))
        response = None
        try:
            startTick = datetime.now()
            mockup_file_path = os.path.join(config['mockup'], URLDest.replace('/redfish/v1/', '', 1).strip('/'), 'index.json')
            if not inService:
                req = requests.get(URLDest, proxies=self.ext_proxies)
                content = req.json if not isXML else req.text
                response = rf.rest.v1.StaticRestResponse(Status=req.status_code, Headers={x:req.headers[x] for x in req.headers}, Content=req.text)
            elif config['mockup'] != '' and os.path.isfile(mockup_file_path):
                content = {}
                with open(mockup_file_path) as mockup_file:
                    content = json.load(mockup_file)
                response = rf.rest.v1.StaticRestResponse(Status=200, Headers={'Content-Type': 'application/json', 'X-Redfish-Mockup': 'true'}, Content=content)
            else:
                response = self.context.get(URLDest, headers=headers)
            elapsed = datetime.now() - startTick
            statusCode = response.status

            traverseLogger.debug('{}, {},\nTIME ELAPSED: {}'.format(statusCode, response.getheaders(), elapsed))
            if statusCode in [200]:
                contenttype = response.getheader('content-type')
                if contenttype is None:
                    traverseLogger.error("Content-type not found in header: {}".format(URILink))
                    contenttype = ''
                if 'application/json' in contenttype:
                    traverseLogger.debug("This is a JSON response")
                    decoded = response.dict
                            
                    # navigate fragment
                    decoded = navigateJsonFragment(decoded, URILink)
                    if decoded is None:
                        traverseLogger.error(
                                "The JSON pointer in the fragment of this URI is not constructed properly: {}".format(URILink))
                elif 'application/xml' in contenttype:
                    decoded = response.text
                elif 'text/xml' in contenttype:
                    # non-service schemas can use "text/xml" Content-Type
                    if inService:
                        traverseLogger.warning(
                                "Incorrect content type 'text/xml' for file within service {}".format(URILink))
                    decoded = response.text
                else:
                    traverseLogger.error(
                            "This URI did NOT return XML or Json contenttype, is this not a Redfish resource (is this redirected?): {}".format(URILink))
                    decoded = None
                    if isXML:
                        traverseLogger.info('Attempting to interpret as XML')
                        decoded = response.text
                    else:
                        try:
                            json.loads(response.text)
                            traverseLogger.info('Attempting to interpret as JSON')
                            decoded = response.dict
                        except ValueError:
                            pass

                return decoded is not None, decoded, response, elapsed
            elif statusCode == 401:
                if inService and AuthType in ['Basic', 'Token']:
                    if AuthType == 'Token':
                        cred_type = 'token'
                    else:
                        cred_type = 'username and password'
                    raise AuthenticationError('Error accessing URI {}. Status code "{} {}". Check {} supplied for "{}" authentication.'
                                              .format(URILink, statusCode, responses[statusCode], cred_type, AuthType))

        except AuthenticationError as e:
            raise e  # re-raise exception
        except Exception as e:
            traverseLogger.error("A problem when getting resource {} has occurred: {}".format(URILink, repr(e)))
            traverseLogger.debug("output: ", exc_info=True)
            if response and response.text:
                traverseLogger.debug("payload: {}".format(response.text))

        if payload is not None:
            return True, payload, response, 0
        return False, None, response, elapsed
