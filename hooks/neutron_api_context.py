# Copyright 2016 Canonical Ltd
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#  http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

from collections import OrderedDict

from charmhelpers.core.hookenv import (
    config,
    relation_ids,
    related_units,
    relation_get,
    log,
)
from charmhelpers.contrib.openstack import context
from charmhelpers.contrib.hahelpers.cluster import (
    determine_api_port,
    determine_apache_port,
)
from charmhelpers.contrib.openstack.utils import (
    os_release,
)

VLAN = 'vlan'
VXLAN = 'vxlan'
GRE = 'gre'
FLAT = 'flat'
LOCAL = 'local'
OVERLAY_NET_TYPES = [VXLAN, GRE]
NON_OVERLAY_NET_TYPES = [VLAN, FLAT, LOCAL]
TENANT_NET_TYPES = [VXLAN, GRE, VLAN, FLAT, LOCAL]


def get_l2population():
    plugin = config('neutron-plugin')
    return config('l2-population') if plugin == "ovs" else False


def _get_overlay_network_type():
    overlay_networks = config('overlay-network-type').split()
    for overlay_net in overlay_networks:
        if overlay_net not in OVERLAY_NET_TYPES:
            raise ValueError('Unsupported overlay-network-type %s'
                             % overlay_net)
    return overlay_networks


def get_overlay_network_type():
    return ','.join(_get_overlay_network_type())


def _get_tenant_network_types():
    default_tenant_network_type = config('default-tenant-network-type')
    tenant_network_types = _get_overlay_network_type()
    tenant_network_types.extend(NON_OVERLAY_NET_TYPES)
    if default_tenant_network_type:
        if (default_tenant_network_type in TENANT_NET_TYPES and
                default_tenant_network_type in tenant_network_types):
            tenant_network_types[:0] = [default_tenant_network_type]
        else:
            raise ValueError('Unsupported or unconfigured '
                             'default-tenant-network-type'
                             ' {}'.format(default_tenant_network_type))
    # Dedupe list but preserve order
    return list(OrderedDict.fromkeys(tenant_network_types))


def get_tenant_network_types():
    '''Get the configured tenant network types

    @return: comma delimited string of configured tenant
             network types.
    '''
    return ','.join(_get_tenant_network_types())


def get_l3ha():
    if config('enable-l3ha'):
        if os_release('neutron-server') < 'juno':
            log('Disabling L3 HA, enable-l3ha is not valid before Juno')
            return False
        if get_l2population():
            log('Disabling L3 HA, l2-population must be disabled with L3 HA')
            return False
        return True
    else:
        return False


def get_dvr():
    if config('enable-dvr'):
        if os_release('neutron-server') < 'juno':
            log('Disabling DVR, enable-dvr is not valid before Juno')
            return False
        if os_release('neutron-server') == 'juno':
            if VXLAN not in config('overlay-network-type').split():
                log('Disabling DVR, enable-dvr requires the use of the vxlan '
                    'overlay network for OpenStack Juno')
                return False
        if get_l3ha():
            log('Disabling DVR, enable-l3ha must be disabled with dvr')
            return False
        if not get_l2population():
            log('Disabling DVR, l2-population must be enabled to use dvr')
            return False
        return True
    else:
        return False


class ApacheSSLContext(context.ApacheSSLContext):

    interfaces = ['https']
    external_ports = []
    service_namespace = 'neutron'

    def __call__(self):
        # late import to work around circular dependency
        from neutron_api_utils import determine_ports
        self.external_ports = determine_ports()
        return super(ApacheSSLContext, self).__call__()


class IdentityServiceContext(context.IdentityServiceContext):

    def __call__(self):
        ctxt = super(IdentityServiceContext, self).__call__()
        if not ctxt:
            return
        ctxt['region'] = config('region')
        return ctxt


class NeutronCCContext(context.NeutronContext):
    interfaces = []

    @property
    def network_manager(self):
        return 'neutron'

    @property
    def plugin(self):
        return config('neutron-plugin')

    @property
    def neutron_security_groups(self):
        return config('neutron-security-groups')

    @property
    def neutron_l2_population(self):
        return get_l2population()

    @property
    def neutron_tenant_network_types(self):
        return get_tenant_network_types()

    @property
    def neutron_overlay_network_type(self):
        return get_overlay_network_type()

    @property
    def neutron_dvr(self):
        return get_dvr()

    @property
    def neutron_l3ha(self):
        return get_l3ha()

    # Do not need the plugin agent installed on the api server
    def _ensure_packages(self):
        pass

    # Do not need the flag on the api server
    def _save_flag_file(self):
        pass

    def get_neutron_api_rel_settings(self):
        settings = {}
        for rid in relation_ids('neutron-api'):
            for unit in related_units(rid):
                rdata = relation_get(rid=rid, unit=unit)
                cell_type = rdata.get('cell_type')
                settings['nova_url'] = rdata.get('nova_url')
                settings['restart_trigger'] = rdata.get('restart_trigger')
                # If there are multiple nova-cloud-controllers joined to this
                # service in a cell deployment then ignore the non-api cell
                # ones
                if cell_type and not cell_type == "api":
                    continue

                if settings['nova_url']:
                    return settings

        return settings

    def __call__(self):
        from neutron_api_utils import api_port
        ctxt = super(NeutronCCContext, self).__call__()
        if config('neutron-plugin') == 'nsx':
            ctxt['nsx_username'] = config('nsx-username')
            ctxt['nsx_password'] = config('nsx-password')
            ctxt['nsx_tz_uuid'] = config('nsx-tz-uuid')
            ctxt['nsx_l3_uuid'] = config('nsx-l3-uuid')
            if 'nsx-controllers' in config():
                ctxt['nsx_controllers'] = \
                    ','.join(config('nsx-controllers').split())
                ctxt['nsx_controllers_list'] = \
                    config('nsx-controllers').split()
        if config('neutron-plugin') == 'plumgrid':
            ctxt['pg_username'] = config('plumgrid-username')
            ctxt['pg_password'] = config('plumgrid-password')
            ctxt['virtual_ip'] = config('plumgrid-virtual-ip')
        elif config('neutron-plugin') == 'midonet':
            ctxt.update(MidonetContext()())
            identity_context = IdentityServiceContext(service='neutron',
                                                      service_user='neutron')()
            if identity_context is not None:
                ctxt.update(identity_context)
        ctxt['l2_population'] = self.neutron_l2_population
        ctxt['enable_dvr'] = self.neutron_dvr
        ctxt['l3_ha'] = self.neutron_l3ha
        if self.neutron_l3ha:
            ctxt['max_l3_agents_per_router'] = \
                config('max-l3-agents-per-router')
            ctxt['min_l3_agents_per_router'] = \
                config('min-l3-agents-per-router')
        ctxt['dhcp_agents_per_network'] = config('dhcp-agents-per-network')
        ctxt['tenant_network_types'] = self.neutron_tenant_network_types
        ctxt['overlay_network_type'] = self.neutron_overlay_network_type
        ctxt['external_network'] = config('neutron-external-network')
        release = os_release('neutron-server')
        if config('neutron-plugin') in ['vsp']:
            _config = config()
            for k, v in _config.iteritems():
                if k.startswith('vsd'):
                    ctxt[k.replace('-', '_')] = v
            for rid in relation_ids('vsd-rest-api'):
                for unit in related_units(rid):
                    rdata = relation_get(rid=rid, unit=unit)
                    vsd_ip = rdata.get('vsd-ip-address')
                    if release >= 'kilo':
                        cms_id_value = rdata.get('nuage-cms-id')
                        log('relation data:cms_id required for'
                            ' nuage plugin: {}'.format(cms_id_value))
                        if cms_id_value is not None:
                            ctxt['vsd_cms_id'] = cms_id_value
                    log('relation data:vsd-ip-address: {}'.format(vsd_ip))
                    if vsd_ip is not None:
                        ctxt['vsd_server'] = '{}:8443'.format(vsd_ip)
            if 'vsd_server' not in ctxt:
                ctxt['vsd_server'] = '1.1.1.1:8443'
        ctxt['verbose'] = config('verbose')
        ctxt['debug'] = config('debug')
        ctxt['neutron_bind_port'] = \
            determine_api_port(api_port('neutron-server'),
                               singlenode_mode=True)
        ctxt['quota_security_group'] = config('quota-security-group')
        ctxt['quota_security_group_rule'] = \
            config('quota-security-group-rule')
        ctxt['quota_network'] = config('quota-network')
        ctxt['quota_subnet'] = config('quota-subnet')
        ctxt['quota_port'] = config('quota-port')
        ctxt['quota_vip'] = config('quota-vip')
        ctxt['quota_pool'] = config('quota-pool')
        ctxt['quota_member'] = config('quota-member')
        ctxt['quota_health_monitors'] = config('quota-health-monitors')
        ctxt['quota_router'] = config('quota-router')
        ctxt['quota_floatingip'] = config('quota-floatingip')

        n_api_settings = self.get_neutron_api_rel_settings()
        if n_api_settings:
            ctxt.update(n_api_settings)

        flat_providers = config('flat-network-providers')
        if flat_providers:
            ctxt['network_providers'] = ','.join(flat_providers.split())

        vlan_ranges = config('vlan-ranges')
        if vlan_ranges:
            ctxt['vlan_ranges'] = ','.join(vlan_ranges.split())

        vni_ranges = config('vni-ranges')
        if vni_ranges:
            ctxt['vni_ranges'] = ','.join(vni_ranges.split())

        ctxt['enable_ml2_port_security'] = config('enable-ml2-port-security')
        ctxt['enable_sriov'] = config('enable-sriov')

        if release == 'kilo' or release >= 'mitaka':
            ctxt['enable_hyperv'] = True
        else:
            ctxt['enable_hyperv'] = False

        if release >= 'mitaka':
            if config('global-physnet-mtu'):
                ctxt['global_physnet_mtu'] = config('global-physnet-mtu')
                if config('path-mtu'):
                    ctxt['path_mtu'] = config('path-mtu')
                else:
                    ctxt['path_mtu'] = config('global-physnet-mtu')

        if config('neutron-plugin') == 'aci':
            identity_context = IdentityServiceContext(service='neutron',
                                    service_user='neutron')()
            if identity_context is not None:
                ctxt.update(identity_context)
            ctxt.update(CiscoAciContext()())

        return ctxt


class HAProxyContext(context.HAProxyContext):
    interfaces = ['ceph']

    def __call__(self):
        '''
        Extends the main charmhelpers HAProxyContext with a port mapping
        specific to this charm.
        Also used to extend nova.conf context with correct api_listening_ports
        '''
        from neutron_api_utils import api_port
        ctxt = super(HAProxyContext, self).__call__()

        # Apache ports
        a_neutron_api = determine_apache_port(api_port('neutron-server'),
                                              singlenode_mode=True)

        port_mapping = {
            'neutron-server': [
                api_port('neutron-server'), a_neutron_api]
        }

        ctxt['neutron_bind_port'] = determine_api_port(
            api_port('neutron-server'),
            singlenode_mode=True,
        )

        # for haproxy.conf
        ctxt['service_ports'] = port_mapping
        return ctxt


class EtcdContext(context.OSContextGenerator):
    interfaces = ['etcd-proxy']

    def __call__(self):
        ctxt = {'cluster': ''}
        cluster_string = ''

        if not config('neutron-plugin') == 'Calico':
            return ctxt

        for rid in relation_ids('etcd-proxy'):
            for unit in related_units(rid):
                rdata = relation_get(rid=rid, unit=unit)
                cluster_string = rdata.get('cluster')
                if cluster_string:
                    break

        ctxt['cluster'] = cluster_string

        return ctxt


class NeutronApiSDNContext(context.SubordinateConfigContext):
    interfaces = 'neutron-plugin-api-subordinate'

    def __init__(self):
        super(NeutronApiSDNContext, self).__init__(
            interface='neutron-plugin-api-subordinate',
            service='neutron-api',
            config_file='/etc/neutron/neutron.conf')

    def __call__(self):
        ctxt = super(NeutronApiSDNContext, self).__call__()
        defaults = {
            'core-plugin': {
                'templ_key': 'core_plugin',
                'value': 'neutron.plugins.ml2.plugin.Ml2Plugin',
            },
            'neutron-plugin-config': {
                'templ_key': 'neutron_plugin_config',
                'value': '/etc/neutron/plugins/ml2/ml2_conf.ini',
            },
            'service-plugins': {
                'templ_key': 'service_plugins',
                'value': 'router,firewall,lbaas,vpnaas,metering',
            },
            'restart-trigger': {
                'templ_key': 'restart_trigger',
                'value': '',
            },
            'quota-driver': {
                'templ_key': 'quota_driver',
                'value': '',
            },
            'api-extensions-path': {
                'templ_key': 'api_extensions_path',
                'value': '',
            },
        }
        for rid in relation_ids('neutron-plugin-api-subordinate'):
            for unit in related_units(rid):
                rdata = relation_get(rid=rid, unit=unit)
                plugin = rdata.get('neutron-plugin')
                if not plugin:
                    continue
                ctxt['neutron_plugin'] = plugin
                for key in defaults.keys():
                    remote_value = rdata.get(key)
                    ctxt_key = defaults[key]['templ_key']
                    if remote_value:
                        ctxt[ctxt_key] = remote_value
                    else:
                        ctxt[ctxt_key] = defaults[key]['value']
                return ctxt
        return ctxt


class NeutronApiSDNConfigFileContext(context.OSContextGenerator):
    interfaces = ['neutron-plugin-api-subordinate']

    def __call__(self):
        for rid in relation_ids('neutron-plugin-api-subordinate'):
            for unit in related_units(rid):
                rdata = relation_get(rid=rid, unit=unit)
                neutron_server_plugin_conf = rdata.get('neutron-plugin-config')
                if neutron_server_plugin_conf:
                    return {'config': neutron_server_plugin_conf}
        return {'config': '/etc/neutron/plugins/ml2/ml2_conf.ini'}


class MidonetContext(context.OSContextGenerator):
    def __init__(self, rel_name='midonet'):
        self.rel_name = rel_name
        self.interfaces = [rel_name]

    def __call__(self):
        for rid in relation_ids(self.rel_name):
            for unit in related_units(rid):
                rdata = relation_get(rid=rid, unit=unit)
                ctxt = {
                    'midonet_api_ip': rdata.get('host'),
                    'midonet_api_port': rdata.get('port'),
                }
                if self.context_complete(ctxt):
                    return ctxt
        return {}

class CiscoAciContext(context.OSContextGenerator):
    def __init__(self, rel_name='neutron-plugin-api-subordinate'):
        self.rel_name = rel_name
        self.interfaces = [rel_name]

    def __call__(self):
        for rid in relation_ids(self.rel_name):
            for unit in related_units(rid):
                rdata = relation_get(rid=rid, unit=unit)
                plugin = rdata.get('neutron_plugin')
                if str(plugin) == "aci":
                    ctxt = {}
                    for k in rdata.keys():
                        ctxt[k] = rdata.get(k)
                    if self.context_complete(ctxt):
                        return ctxt
        return {}

