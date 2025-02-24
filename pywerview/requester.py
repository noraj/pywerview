# This file is part of PywerView.

# PywerView is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.

# PywerView is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.

# You should have received a copy of the GNU General Public License
# along with PywerView.  If not, see <http://www.gnu.org/licenses/>.

# Yannick Méheut [yannick (at) meheut (dot) org] - Copyright © 2022

import sys
import logging
import socket
import ntpath
import ldap3
import os
import tempfile

from ldap3.protocol.formatters.formatters import *

from impacket.smbconnection import SMBConnection
from impacket.smbconnection import SessionError
from impacket.krb5.ccache import CCache, Credential, CountedOctetString
from impacket.krb5 import constants
from impacket.krb5.types import Principal
from impacket.dcerpc.v5.rpcrt import RPC_C_AUTHN_LEVEL_PKT_PRIVACY
from impacket.dcerpc.v5 import transport, wkst, srvs, samr, scmr, drsuapi, epm
from impacket.dcerpc.v5.dcom import wmi
from impacket.dcerpc.v5.dtypes import NULL
from impacket.dcerpc.v5.dcomrt import DCOMConnection
from impacket.dcerpc.v5.rpcrt import DCERPCException

import pywerview.formatters as fmt

class LDAPRequester():
    def __init__(self, domain_controller, domain=str(), user=(), password=str(),
                 lmhash=str(), nthash=str(), do_kerberos=False, do_tls=False):
        self._domain_controller = domain_controller
        self._domain = domain
        self._user = user
        self._password = password
        self._lmhash = lmhash
        self._nthash = nthash
        self._do_kerberos = do_kerberos
        self._do_tls = do_tls
        self._queried_domain = None
        self._ads_path = None
        self._ads_prefix = None
        self._ldap_connection = None
        self._base_dn = None

        logger = logging.getLogger('pywerview_main_logger.LDAPRequester')
        self._logger = logger

    def _get_netfqdn(self):
        try:
            smb = SMBConnection(self._domain_controller, self._domain_controller)
        except socket.error:
            self._logger.warning('Socket error when opening the SMB connection')
            return str()

        self._logger.debug('SMB loging parameters : user = {0}  / password = {1} / domain = {2} '
                           '/ LM hash = {3} / NT hash = {4}'.format(self._user, self._password,
                                                                    self._domain, self._lmhash,
                                                                    self._nthash))

        smb.login(self._user, self._password, domain=self._domain,
                lmhash=self._lmhash, nthash=self._nthash)
        fqdn = smb.getServerDNSDomainName()
        smb.logoff()

        return fqdn

    def _patch_spn(self, creds, principal):
        self._logger.debug('Patching principal to {}'.format(principal))

        from pyasn1.codec.der import decoder, encoder
        from impacket.krb5.asn1 import TGS_REP, Ticket

        # Code is ~~based on~~ stolen from https://github.com/SecureAuthCorp/impacket/pull/1256
        tgs = creds.toTGS(principal)
        decoded_st = decoder.decode(tgs['KDC_REP'], asn1Spec=TGS_REP())[0]
        decoded_st['ticket']['sname']['name-string'][0] = 'ldap'
        decoded_st['ticket']['sname']['name-string'][1] = self._domain_controller.lower()
        decoded_st['ticket']['realm'] = self._queried_domain.upper()

        new_creds = Credential(data=creds.getData())
        new_creds.ticket = CountedOctetString()
        new_creds.ticket['data'] = encoder.encode(decoded_st['ticket'].clone(tagSet=Ticket.tagSet, cloneValueFlag=True))
        new_creds.ticket['length'] = len(new_creds.ticket['data'])
        new_creds['server'].fromPrincipal(Principal(principal, type=constants.PrincipalNameType.NT_PRINCIPAL.value))

        return new_creds

    def _create_ldap_connection(self, queried_domain=str(), ads_path=str(),
                                ads_prefix=str()):
        if not self._domain:
            if self._do_kerberos:
                ccache = CCache.loadFile(os.getenv('KRB5CCNAME'))
                self._domain = ccache.principal.realm['data'].decode('utf-8')
            else:
                try:
                    self._domain = self._get_netfqdn()
                except SessionError as e:
                    self._logger.critical(e)
                    sys.exit(-1)

        if not queried_domain:
            if self._do_kerberos:
                ccache = CCache.loadFile(os.getenv('KRB5CCNAME'))
                queried_domain = ccache.principal.realm['data'].decode('utf-8')
            else:
                try:
                    queried_domain = self._get_netfqdn()
                except SessionError as e:
                    self._logger.critical(e)
                    sys.exit(-1)
        self._queried_domain = queried_domain

        base_dn = str()

        if ads_prefix:
            self._ads_prefix = ads_prefix
            base_dn = '{},'.format(self._ads_prefix)

        if ads_path:
            # TODO: manage ADS path starting with 'GC://'
            if ads_path.upper().startswith('LDAP://'):
                ads_path = ads_path[7:]
            self._ads_path = ads_path
            base_dn += self._ads_path
        else:
            base_dn += ','.join('dc={}'.format(x) for x in self._queried_domain.split('.'))

        # base_dn is no longer used within `_create_ldap_connection()`, but I don't want to break
        # the function call. So we store it in an attriute and use it in `_ldap_search()`
        self._base_dn = base_dn

        # Format the username and the domain
        # ldap3 seems not compatible with USER@DOMAIN format
        if self._do_kerberos:
            user = '{}@{}'.format(self._user, self._domain.upper())
        else:
            user = '{}\\{}'.format(self._domain, self._user)

        # Call custom formatters for several AD attributes
        formatter = {'userAccountControl': fmt.format_useraccountcontrol,
                'trustType': fmt.format_trusttype,
                'trustDirection': fmt.format_trustdirection,
                'trustAttributes': fmt.format_trustattributes,
                'msDS-MaximumPasswordAge': format_ad_timedelta,
                'msDS-MinimumPasswordAge': format_ad_timedelta,
                'msDS-LockoutDuration': format_ad_timedelta,
                'msDS-LockoutObservationWindow': format_ad_timedelta,
                'msDS-GroupMSAMembership': fmt.format_groupmsamembership,
                'msDS-ManagedPassword': fmt.format_managedpassword}

        if self._do_tls:
            ldap_scheme = 'ldaps'
            self._logger.debug('LDAPS connection forced')
        else:
            ldap_scheme = 'ldap'
        ldap_server = ldap3.Server('{}://{}'.format(ldap_scheme, self._domain_controller), formatter=formatter)
        ldap_connection_kwargs = {'user': user, 'raise_exceptions': True}

        # We build the authentication arguments depending on auth mode
        if self._do_kerberos:
            self._logger.debug('LDAP authentication with Keberos')
            ldap_connection_kwargs['authentication'] = ldap3.SASL
            ldap_connection_kwargs['sasl_mechanism'] = ldap3.KERBEROS

            # Verifying if we have the correct TGS/TGT to interrogate the LDAP server
            ccache = CCache.loadFile(os.getenv('KRB5CCNAME'))
            principal = 'ldap/{}@{}'.format(self._domain_controller.lower(), self._queried_domain.upper())

            # We look for the TGS with the right SPN
            creds = ccache.getCredential(principal, anySPN=False)
            if creds:
                self._logger.debug('TGS found in KRB5CCNAME file')
                if creds['server'].prettyPrint().lower() != creds['server'].prettyPrint():
                    self._logger.debug('SPN not in lowercase, patching SPN')
                    new_creds = self._patch_spn(creds, principal)
                    # We build a new CCache with the new ticket
                    ccache.credentials.append(new_creds)
                    temp_ccache = tempfile.NamedTemporaryFile()
                    ccache.saveFile(temp_ccache.name)
                    cred_store = {'ccache': 'FILE:{}'.format(temp_ccache.name)}
                else:
                    cred_store = dict()
            else:
                self._logger.debug('TGS not found in KRB5CCNAME, looking for '
                        'TGS with alternative SPN')
                # If we don't find it, we search for any SPN
                creds = ccache.getCredential(principal, anySPN=True)
                if creds:
                    # If we find one, we build a custom TGS
                    self._logger.debug('Alternative TGS found, patching SPN')
                    new_creds = self._patch_spn(creds, principal)
                    # We build a new CCache with the new ticket
                    ccache.credentials.append(new_creds)
                    temp_ccache = tempfile.NamedTemporaryFile()
                    ccache.saveFile(temp_ccache.name)
                    cred_store = {'ccache': 'FILE:{}'.format(temp_ccache.name)}
                else:
                    # If we don't find any, we hope for the best (TGT in cache)
                    self._logger.debug('Alternative TGS not found, using KRB5CCNAME as is '
                            'while hoping it contains a TGT')
                    cred_store = dict()
            ldap_connection_kwargs['cred_store'] = cred_store
            self._logger.debug('LDAP binding parameters: server = {0} / user = {1} '
                   '/ Kerberos auth'.format(self._domain_controller, user))
        else:
            self._logger.debug('LDAP authentication with NTLM')
            ldap_connection_kwargs['authentication'] = ldap3.NTLM
            if self._lmhash and self._nthash:
                ldap_connection_kwargs['password'] = '{}:{}'.format(self._lmhash, self._nthash)
                self._logger.debug('LDAP binding parameters: server = {0} / user = {1} '
                   '/ hash = {2}'.format(self._domain_controller, user, ldap_connection_kwargs['password']))
            else:
                ldap_connection_kwargs['password'] = self._password
                self._logger.debug('LDAP binding parameters: server = {0} / user = {1} '
                   '/ password = {2}'.format(self._domain_controller, user, ldap_connection_kwargs['password']))

        try:
            ldap_connection = ldap3.Connection(ldap_server, **ldap_connection_kwargs)
            try:
                ldap_connection.bind()
            except ldap3.core.exceptions.LDAPSocketOpenError as e:
                self._logger.critical(e)
                if self._do_tls:
                    self._logger.critical('TLS negociation failed, this error is mostly due to your host '
                                          'not supporting SHA1 as signing algorithm for certificates')
                sys.exit(-1)
        except ldap3.core.exceptions.LDAPStrongerAuthRequiredResult:
            # We need to try TLS
            self._logger.warning('Server returns LDAPStrongerAuthRequiredResult, falling back to LDAPS')
            ldap_server = ldap3.Server('ldaps://{}'.format(self._domain_controller), formatter=formatter)
            ldap_connection = ldap3.Connection(ldap_server, **ldap_connection_kwargs)
            try:
                ldap_connection.bind()
            except ldap3.core.exceptions.LDAPSocketOpenError as e:
                self._logger.critical(e)
                self._logger.critical('TLS negociation failed, this error is mostly due to your host '
                                      'not supporting SHA1 as signing algorithm for certificates')
                sys.exit(-1)

        self._ldap_connection = ldap_connection

    def _ldap_search(self, search_filter, class_result, attributes=list(), controls=list()):
        results = list()

        # if no attribute name specified, we return all attributes
        if not attributes:
            attributes =  ldap3.ALL_ATTRIBUTES

        self._logger.debug('search_base = {0} / search_filter = {1} / attributes = {2}'.format(self._base_dn,
                                                                                               search_filter,
                                                                                               attributes))

        # Microsoft Active Directory set an hard limit of 1000 entries returned by any search
        search_results=self._ldap_connection.extend.standard.paged_search(search_base=self._base_dn,
                search_filter=search_filter, attributes=attributes,
                controls=controls, paged_size=1000, generator=True)

        try:
            # Skip searchResRef
            for result in search_results:
                if result['type'] != 'searchResEntry':
                    continue
                results.append(class_result(result['attributes']))

        except ldap3.core.exceptions.LDAPAttributeError as e:
            self._logger.critical(e)
            sys.exit(-1)

        if not results:
            self._logger.debug('Query returned an empty result')

        return results

    @staticmethod
    def _ldap_connection_init(f):
        def wrapper(*args, **kwargs):
            instance = args[0]
            queried_domain = kwargs.get('queried_domain', None)
            ads_path = kwargs.get('ads_path', None)
            ads_prefix = kwargs.get('ads_prefix', None)
            if (not instance._ldap_connection) or \
               (queried_domain != instance._queried_domain) or \
               (ads_path != instance._ads_path) or \
               (ads_prefix != instance._ads_prefix):
                if instance._ldap_connection:
                    instance._ldap_connection.unbind()
                instance._create_ldap_connection(queried_domain=queried_domain,
                                                 ads_path=ads_path, ads_prefix=ads_prefix)
            return f(*args, **kwargs)
        return wrapper

    def __enter__(self):
        self._create_ldap_connection()
        return self

    def __exit__(self, type, value, traceback):
        try:
            self._ldap_connection.unbind()
        except AttributeError:
            self._logger.warning('Error when unbinding')
            pass
        self._ldap_connection = None

class RPCRequester():
    def __init__(self, target_computer, domain=str(), user=(), password=str(),
                 lmhash=str(), nthash=str(), do_kerberos=False):
        self._target_computer = target_computer
        self._domain = domain
        self._user = user
        self._password = password
        self._lmhash = lmhash
        self._nthash = nthash
        self._do_kerberos = do_kerberos
        self._pipe = None
        self._rpc_connection = None
        self._dcom = None
        self._wmi_connection = None

        logger = logging.getLogger('pywerview_main_logger.RPCRequester')
        self._logger = logger

    def _create_rpc_connection(self, pipe):
        # Here we build the DCE/RPC connection
        self._pipe = pipe

        binding_strings = dict()
        binding_strings['srvsvc'] = srvs.MSRPC_UUID_SRVS
        binding_strings['wkssvc'] = wkst.MSRPC_UUID_WKST
        binding_strings['samr'] = samr.MSRPC_UUID_SAMR
        binding_strings['svcctl'] = scmr.MSRPC_UUID_SCMR
        binding_strings['drsuapi'] = drsuapi.MSRPC_UUID_DRSUAPI

        # TODO: try to fallback to TCP/139 if tcp/445 is closed
        if self._pipe == r'\drsuapi':
            string_binding = epm.hept_map(self._target_computer, drsuapi.MSRPC_UUID_DRSUAPI,
                                          protocol='ncacn_ip_tcp')
            rpctransport = transport.DCERPCTransportFactory(string_binding)
            rpctransport.set_credentials(username=self._user, password=self._password,
                                         domain=self._domain, lmhash=self._lmhash,
                                         nthash=self._nthash)
        else:
            rpctransport = transport.SMBTransport(self._target_computer, 445, self._pipe,
                                                  username=self._user, password=self._password,
                                                  domain=self._domain, lmhash=self._lmhash,
                                                  nthash=self._nthash, doKerberos=self._do_kerberos)

        rpctransport.set_connect_timeout(10)
        dce = rpctransport.get_dce_rpc()

        if self._pipe == r'\drsuapi':
            dce.set_auth_level(RPC_C_AUTHN_LEVEL_PKT_PRIVACY)

        try:
            dce.connect()
        except Exception as e:
            self._logger.critical('Error when creating RPC connection')
            self._logger.critical(e)
            self._rpc_connection = None
        else:
            dce.bind(binding_strings[self._pipe[1:]])
            self._rpc_connection = dce

    def _create_wmi_connection(self, namespace='root\\cimv2'):
        try:
            self._dcom = DCOMConnection(self._target_computer, self._user, self._password,
                                        self._domain, self._lmhash, self._nthash, doKerberos=self._do_kerberos)
        except Exception as e:
            self._logger.critical('Error when creating WMI connection')
            self._logger.critical(e)
            self._dcom = None
        else:
            i_interface = self._dcom.CoCreateInstanceEx(wmi.CLSID_WbemLevel1Login,
                                                        wmi.IID_IWbemLevel1Login)
            i_wbem_level1_login = wmi.IWbemLevel1Login(i_interface)
            self._wmi_connection = i_wbem_level1_login.NTLMLogin(ntpath.join('\\\\{}\\'.format(self._target_computer), namespace),
                                                                 NULL, NULL)

    @staticmethod
    def _rpc_connection_init(pipe=r'\srvsvc'):
        def decorator(f):
            def wrapper(*args, **kwargs):
                instance = args[0]
                if (not instance._rpc_connection) or (pipe != instance._pipe):
                    if instance._rpc_connection:
                        instance._rpc_connection.disconnect()
                    instance._create_rpc_connection(pipe=pipe)
                if instance._rpc_connection is None:
                    return None
                return f(*args, **kwargs)
            return wrapper
        return decorator

    @staticmethod
    def _wmi_connection_init():
        def decorator(f):
            def wrapper(*args, **kwargs):
                instance = args[0]
                if not instance._wmi_connection:
                    instance._create_wmi_connection()
                if instance._dcom is None:
                    return None
                return f(*args, **kwargs)
            return wrapper
        return decorator

    def __enter__(self):
        # Picked because it's the most used by the net* functions
        self._create_rpc_connection(r'\srvsvc')
        return self

    def __exit__(self, type, value, traceback):
        try:
            self._rpc_connection.disconnect()
        except AttributeError:
            pass
        self._rpc_connection = None

class LDAPRPCRequester(LDAPRequester, RPCRequester):
    def __init__(self, target_computer, domain=str(), user=(), password=str(),
                 lmhash=str(), nthash=str(), do_kerberos=False, do_tls=False,
                 domain_controller=str()):
        # If no domain controller was given, we assume that the user wants to
        # target a domain controller to perform LDAP requests against
        if not domain_controller:
            domain_controller = target_computer
        LDAPRequester.__init__(self, domain_controller, domain, user, password,
                               lmhash, nthash, do_kerberos, do_tls)
        RPCRequester.__init__(self, target_computer, domain, user, password,
                               lmhash, nthash, do_kerberos)

        logger = logging.getLogger('pywerview_main_logger.LDAPRPCRequester')
        self._logger = logger

    def __enter__(self):
        try:
            LDAPRequester.__enter__(self)
        except (socket.error, IndexError):
            pass
        # This should work every time
        RPCRequester.__enter__(self)

        return self

    def __exit__(self, type, value, traceback):
        LDAPRequester.__exit__(self, type, value, traceback)
        RPCRequester.__exit__(self, type, value, traceback)

