import boto3
import botocore
import json
import logging
import os
from threading import Lock
from time import sleep


class ClientFactory(object):

    """Manages creating and caching boto3 clients, helpful when creating lots of
    clients in different regions or functions.

    Example usage:

    from tackcat import utils

    class MyClass(object):
        def __init__(self):
            self._boto_client = utils.ClientFactory()
        def my_function(self):
            s3_client = self._boto_client.get('s3', region='us-west-2')
            return s3_client.list_buckets()
    """

    def __init__(self, logger=None, loglevel='error', botolevel='error'):
        """Sets up the cache dict, a locking mechanism and the logging object

        Args:
            logger (obj): a logging instance
        """

        self._clients = {"default_role": {}}
        self._lock = Lock()
        if not logger:
            loglevel = getattr(logging, loglevel.upper(), 20)
            botolevel = getattr(logging, botolevel.upper(), 40)
            mainlogger = logging.getLogger()
            mainlogger.setLevel(loglevel)
            logging.getLogger('boto3').setLevel(botolevel)
            logging.getLogger('botocore').setLevel(botolevel)
            logging.getLogger('nose').setLevel(botolevel)
            logging.getLogger('s3transfer').setLevel(botolevel)
            if len(mainlogger.handlers) == 0:
                mainlogger.addHandler(logging.StreamHandler())
        else:
            self.logger = logger
        return

    def get(self, service, region=None, role='default_role', access_key=None, secret_key=None,
            session_token=None, s3v4=False):
        """get a client for a given service and region, optionally with specific role, credentials and/or sig version

        Args:
            service (str): service name
            region (str): [optional] region name, defaults to current region
            role (str): [optional] descriptive role name used to seperate different sets of credentials for the same service/region, defaults to default_role which uses the lambda execution role
            access_key (str): [optional] IAM access key, defaults to None (uses execution role creds)
            secret_key (str): [optional] IAM secret key, defaults to None (uses execution role creds)
            session_token (str): [optional] IAM session token, defaults to None (uses execution role creds)
            s3v4 (bool): [optional] when True enables signature_version=s3v4 which is required for SSE protected buckets/objects

        Returns:
            class: boto3 client
        """

        if not region:
            self.logger.debug("Region not set explicitly, getting default region")
            region = os.environ['AWS_DEFAULT_REGION']
        s3v4 = 's3v4' if s3v4 else 'default_sig_version'
        try:
            self.logger.debug("Trying to get [%s][%s][%s][%s]" % (role, region, service, s3v4))
            client = self._clients[role][region][service][s3v4]
            if access_key:
                if self._clients[role][region]['session'].get_credentials().access_key != access_key:
                    self.logger.debug("credentials changed, forcing update...")
                    raise KeyError("New credentials for this role, need a new session.")
            return client
        except KeyError:
            self.logger.debug("Couldn't return an existing client, making a new one...")
            if role not in self._clients.keys():
                self._clients[role] = {}
            if region not in self._clients[role].keys():
                self._clients[role][region] = {}
            if service not in self._clients[role].keys():
                self._clients[role][region][service] = {}
            if 'session' not in self._clients[role][region].keys():
                self._clients[role][region]['session'] = self._create_session(region, access_key, secret_key,
                                                                              session_token)
            self._clients[role][region][service][s3v4] = self._create_client(role, region, service, s3v4)
            return self._clients[role][region][service][s3v4]

    def _create_session(self, region, access_key, secret_key, session_token):
        """creates (or fetches from cache) a boto3 session object

        Args:
            region (str): region name
            access_key (str): [optional] IAM secret key, defaults to None (uses execution role creds)
            secret_key (str): [optional] IAM secret key, defaults to None (uses execution role creds)
            session_token (str): [optional] IAM secret key, defaults to None (uses execution role creds)
        """
        session = None
        retry = 0
        max_retries = 4
        while not session:
            try:
                with self._lock:
                    if access_key and secret_key and session_token:
                        session = boto3.session.Session(
                            aws_access_key_id=access_key,
                            aws_secret_access_key=secret_key,
                            aws_session_token=session_token,
                            region_name=region
                        )
                    else:
                        session = boto3.session.Session(region_name=region)
                return session
            except Exception:
                self.logger.debug("failed to create session", exc_info=1)
                retry += 1
                if retry >= max_retries:
                    raise
                sleep(5*(retry**2))

    def _create_client(self, role, region, service, s3v4):
        """creates (or fetches from cache) a boto3 client object

        Args:
            role (str): role descriptor
            region (str): region name
            service (str): AWS service name
            s3v4 (bool): when True enables signature_version=s3v4 which is required for SSE protected buckets/objects
        """
        client = None
        retry = 0
        max_retries = 4
        while not client:
            try:
                with self._lock:
                    if s3v4 == 's3v4':
                        client = self._clients[role][region]['session'].client(
                            service,
                            config=botocore.client.Config(signature_version='s3v4')
                        )
                    else:
                        client = self._clients[role][region]['session'].client(service)
                return client
            except Exception:
                self.logger.debug("failed to create client", exc_info=1)
                retry += 1
                if retry >= max_retries:
                    raise
                sleep(5*(retry**2))

    def get_available_regions(self, service):
        """fetches available regions for a service

        Args:
            service (str): AWS service name

        Returns:
            list: aws region name strings
        """

        for role in self._clients.keys():
            for region in self._clients[role].keys():
                if 'session' in self._clients[role][region].keys():
                    return self._clients[role][region]['session'].get_available_regions(service)
        session = boto3.session.Session()
        return session.get_available_regions(service)


class Logger(object):

    """Wrapper for a logging object that logs in json"""

    def __init__(self, request_id=None, log_format='json', loglevel='warning', botolevel='critical'):
        """Initializes logging with request_id"""
        self.request_id = request_id
        self.log_format = log_format
        self.config(request_id, loglevel=loglevel, botolevel=botolevel)
        return

    def config(self, request_id=None, original_job_id=None, job_id=None,
               artifact_revision_id=None, pipeline_execution_id=None, pipeline_action=None,
               stage_name=None, pipeline_name=None, loglevel='warning', botolevel='critical'):
        """Configures logging object

        Args:
            request_id (str): request id.
            original_job_id (str): [optional] pipeline job_id from first request in this run.
            job_id (str): [optional] pipeline job_id for the current invocation (differs from original_job_id if this is a continuation invocation).
            artifact_revision_id (str): [optional] commit id for current revision.
            pipeline_execution_id (str): [optional] pipeline execution id (same for all actions/stages in this pipeline run).
            pipeline_action (str): [optional] pipeline action name.
            stage_name (str): [optional] pipeline stage name.
            pipeline_name (str): [optional] pipeline name.
            loglevel (str): [optional] logging verbosity, defaults to warning.
            botolevel (str): [optional] boto logging verbosity, defaults to critical.
        """

        loglevel = getattr(logging, loglevel.upper(), 20)
        botolevel = getattr(logging, botolevel.upper(), 40)
        mainlogger = logging.getLogger()
        mainlogger.setLevel(loglevel)
        logging.getLogger('boto3').setLevel(botolevel)
        logging.getLogger('botocore').setLevel(botolevel)
        logging.getLogger('nose').setLevel(botolevel)
        logging.getLogger('s3transfer').setLevel(botolevel)
        if self.log_format == 'json':
            logfmt = '{"time_stamp": "%(asctime)s", "log_level": "%(levelname)s", "data": %(message)s}\n'
        elif self.log_format == 'logfile':
            logfmt = '%(asctime)s %(levelname)s %(message)s\n'
        else:
            logfmt = '%(message)s\n'
        if len(mainlogger.handlers) == 0:
            mainlogger.addHandler(logging.StreamHandler())
        mainlogger.handlers[0].setFormatter(logging.Formatter(logfmt))
        self.log = logging.LoggerAdapter(mainlogger, {})
        self.request_id = request_id
        self.original_job_id = original_job_id
        self.job_id = job_id
        self.pipeline_execution_id = pipeline_execution_id
        self.artifact_revision_id = artifact_revision_id
        self.pipeline_action = pipeline_action
        self.stage_name = stage_name
        self.pipeline_name = pipeline_name

    def set_boto_level(self, botolevel):
        """sets boto logging level

        Args:
        botolevel (str): boto3 logging verbosity (critical|error|warning|info|debug)
        """

        botolevel = getattr(logging, botolevel.upper(), 40)
        logging.getLogger('boto3').setLevel(botolevel)
        logging.getLogger('botocore').setLevel(botolevel)
        logging.getLogger('nose').setLevel(botolevel)
        logging.getLogger('s3transfer').setLevel(botolevel)
        return

    def _format(self, message):
        if self.log_format == 'json':
            message = self._format_json(message)
        else:
            message = str(message)
        print(message)
        return message

    def _format_json(self, message):
        """formats log message in json

        Args:
        message (str): log message, can be a dict, list, string, or json blob
        """

        metadata = {}
        if self.request_id:
            metadata["request_id"] = self.request_id
        if self.original_job_id:
            metadata["original_job_id"] = self.original_job_id
        if self.pipeline_execution_id:
            metadata["pipeline_execution_id"] = self.pipeline_execution_id
        if self.pipeline_name:
            metadata["pipeline_name"] = self.pipeline_name
        if self.stage_name:
            metadata["stage_name"] = self.stage_name
        if self.artifact_revision_id:
            metadata["artifact_revision_id"] = self.artifact_revision_id
        if self.pipeline_action:
            metadata["pipeline_action"] = self.pipeline_action
        if self.job_id:
            metadata["job_id"] = self.job_id
        try:
            message = json.loads(message)
        except Exception:
            pass
        try:
            metadata["message"] = message
            return json.dumps(metadata)
        except Exception:
            metadata["message"] = str(message)
            return json.dumps(metadata)

    def debug(self, message, **kwargs):
        """wrapper for logging.debug call"""
        self.log.debug(self._format(message), **kwargs)

    def info(self, message, **kwargs):
        """wrapper for logging.info call"""
        self.log.info(self._format(message), **kwargs)

    def warning(self, message, **kwargs):
        """wrapper for logging.warning call"""
        self.log.warning(self._format(message), **kwargs)

    def error(self, message, **kwargs):
        """wrapper for logging.error call"""
        self.log.error(self._format(message), **kwargs)

    def critical(self, message, **kwargs):
        """wrapper for logging.critical call"""
        self.log.critical(self._format(message), **kwargs)
