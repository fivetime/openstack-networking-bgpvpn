# Copyright (c) 2024 OpenStack Foundation.
# All Rights Reserved.
#
#    Licensed under the Apache License, Version 2.0 (the "License"); you may
#    not use this file except in compliance with the License. You may obtain
#    a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#    WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#    License for the specific language governing permissions and limitations
#    under the License.

"""OVN BGPVPN Service Driver

This driver integrates networking-bgpvpn with OVN by writing EVPN metadata
into the OVN NB database. The OVN BGP Agent reads this metadata and
configures FRR/kernel accordingly.

Supports:
- L2 EVPN (EVPN-VPLS)
- L3 EVPN (EVPN-VRF with Type-5 routes)
- Network associations
- Router associations
- Port associations (with routes control extension)
"""

from neutron_lib.api.definitions import bgpvpn_routes_control as bgpvpn_rc_def
from neutron_lib.api.definitions import bgpvpn_vni as bgpvpn_vni_def
from neutron_lib.callbacks import events
from neutron_lib.callbacks import registry
from neutron_lib.callbacks import resources
from neutron_lib import constants as const
from neutron_lib.plugins import directory

from oslo_log import helpers as log_helpers
from oslo_log import log as logging

from networking_bgpvpn.neutron.extensions import bgpvpn as bgpvpn_ext
from networking_bgpvpn.neutron.services.common import constants as bgpvpn_const
from networking_bgpvpn.neutron.services.service_drivers import driver_api

LOG = logging.getLogger(__name__)

OVN_DRIVER_NAME = "ovn"


def _log_callback_processing_exception(resource, event, trigger, payload, e):
    LOG.exception("Error during notification processing "
                  "%(resource)s %(event)s, %(trigger)s, "
                  "%(payload)s: %(exc)s",
                  {'trigger': trigger,
                   'resource': resource,
                   'event': event,
                   'payload': payload,
                   'exc': e})


@registry.has_registry_receivers
class OVNBGPVPNDriver(driver_api.BGPVPNDriverRC):
    """OVN BGPVPN Service Driver

    This driver writes EVPN configuration into OVN NB database external_ids.
    The OVN BGP Agent monitors these fields and configures the data plane.
    """

    more_supported_extension_aliases = [
        bgpvpn_rc_def.ALIAS,
        bgpvpn_vni_def.ALIAS
    ]

    def __init__(self, service_plugin):
        super().__init__(service_plugin)
        self._ovn_client = None

    @property
    def ovn_client(self):
        if self._ovn_client is None:
            # Lazy initialization of OVN client
            from networking_bgpvpn.neutron.services.service_drivers.ovn \
                import ovn_client
            self._ovn_client = ovn_client.OVNClient()
        return self._ovn_client

    def _validate_bgpvpn_type(self, bgpvpn):
        """Validate BGPVPN type is supported"""
        if bgpvpn['type'] not in [bgpvpn_const.BGPVPN_L2,
                                  bgpvpn_const.BGPVPN_L3]:
            raise bgpvpn_ext.BGPVPNTypeNotSupported(
                driver=OVN_DRIVER_NAME,
                type=bgpvpn['type'])

    def _validate_vni_required(self, bgpvpn):
        """Validate VNI is provided for OVN driver"""
        if not bgpvpn.get(bgpvpn_vni_def.VNI):
            raise bgpvpn_ext.BGPVPNDriverError(
                method="OVN driver requires VNI to be specified")

    def _common_precommit_checks(self, bgpvpn):
        """Common validation for BGPVPN operations"""
        self._validate_bgpvpn_type(bgpvpn)
        self._validate_vni_required(bgpvpn)

    # =========================================================================
    # BGPVPN CRUD operations
    # =========================================================================

    def create_bgpvpn_precommit(self, context, bgpvpn):
        """Validate BGPVPN creation"""
        self._common_precommit_checks(bgpvpn)

    def create_bgpvpn_postcommit(self, context, bgpvpn):
        """No-op: EVPN config applied when associations are created"""
        LOG.debug("Created BGPVPN %s (type=%s, vni=%s)",
                  bgpvpn['id'], bgpvpn['type'],
                  bgpvpn.get(bgpvpn_vni_def.VNI))

    def update_bgpvpn_precommit(self, context, old_bgpvpn, new_bgpvpn):
        """Validate BGPVPN update"""
        self._common_precommit_checks(new_bgpvpn)

    def update_bgpvpn_postcommit(self, context, old_bgpvpn, new_bgpvpn):
        """Update EVPN configuration in OVN for associated resources"""
        from networking_bgpvpn.neutron.services.common import utils

        (added_keys, removed_keys, changed_keys) = (
            utils.get_bgpvpn_differences(new_bgpvpn, old_bgpvpn))

        # Attributes that don't require OVN update
        ATTRIBUTES_TO_IGNORE = {'name', 'tenant_id', 'project_id'}
        moving_keys = added_keys | removed_keys | changed_keys

        if not (moving_keys - ATTRIBUTES_TO_IGNORE):
            return

        LOG.info("Updating BGPVPN %s, changed attributes: %s",
                 new_bgpvpn['id'], moving_keys - ATTRIBUTES_TO_IGNORE)

        # Update all associated networks
        for network_id in new_bgpvpn.get('networks', []):
            self.ovn_client.update_logical_switch_evpn_config(
                context, network_id, new_bgpvpn)

        # Update all associated routers (affects their connected networks)
        for router_id in new_bgpvpn.get('routers', []):
            self._update_router_evpn_config(context, router_id, new_bgpvpn)

    def delete_bgpvpn_precommit(self, context, bgpvpn):
        """Remove EVPN configuration from OVN before deletion"""
        LOG.info("Deleting BGPVPN %s, removing EVPN config from OVN",
                 bgpvpn['id'])

        # Remove config from associated networks
        for network_id in bgpvpn.get('networks', []):
            self.ovn_client.clear_logical_switch_evpn_config(
                context, network_id)

        # Remove config from router-connected networks
        for router_id in bgpvpn.get('routers', []):
            self._clear_router_evpn_config(context, router_id)

    def delete_bgpvpn_postcommit(self, context, bgpvpn):
        """Post-deletion cleanup (if needed)"""
        pass

    # =========================================================================
    # Network Association operations
    # =========================================================================

    def create_net_assoc_postcommit(self, context, net_assoc):
        """Apply EVPN configuration to Logical_Switch in OVN NB"""
        bgpvpn = self.get_bgpvpn(context, net_assoc['bgpvpn_id'])
        network_id = net_assoc['network_id']

        LOG.info("Creating network association: BGPVPN %s <-> Network %s",
                 bgpvpn['id'], network_id)

        self.ovn_client.update_logical_switch_evpn_config(
            context, network_id, bgpvpn)

    def delete_net_assoc_precommit(self, context, net_assoc):
        """Remove EVPN configuration from Logical_Switch"""
        network_id = net_assoc['network_id']

        LOG.info("Deleting network association for network %s", network_id)

        # Check if network is still associated via router
        if not self._network_has_router_bgpvpn(context, network_id):
            self.ovn_client.clear_logical_switch_evpn_config(
                context, network_id)

    def delete_net_assoc_postcommit(self, context, net_assoc):
        """Post-deletion cleanup"""
        pass

    # =========================================================================
    # Router Association operations
    # =========================================================================

    def create_router_assoc_postcommit(self, context, router_assoc):
        """Apply EVPN to all networks connected to this router"""
        bgpvpn = self.get_bgpvpn(context, router_assoc['bgpvpn_id'])
        router_id = router_assoc['router_id']

        if bgpvpn['type'] != bgpvpn_const.BGPVPN_L3:
            raise bgpvpn_ext.BGPVPNDriverError(
                method="Router associations require L3 BGPVPN type")

        LOG.info("Creating router association: BGPVPN %s <-> Router %s",
                 bgpvpn['id'], router_id)

        self._update_router_evpn_config(context, router_id, bgpvpn)

    def update_router_assoc_postcommit(self, context, old_router_assoc,
                                       router_assoc):
        """Update router association (e.g., advertise_extra_routes)"""
        bgpvpn = self.get_bgpvpn(context, router_assoc['bgpvpn_id'])
        router_id = router_assoc['router_id']

        LOG.info("Updating router association for router %s", router_id)

        self._update_router_evpn_config(context, router_id, bgpvpn)

    def delete_router_assoc_precommit(self, context, router_assoc):
        """Remove EVPN from router-connected networks"""
        router_id = router_assoc['router_id']

        LOG.info("Deleting router association for router %s", router_id)

        self._clear_router_evpn_config(context, router_id)

    def delete_router_assoc_postcommit(self, context, router_assoc):
        """Post-deletion cleanup"""
        pass

    # =========================================================================
    # Port Association operations (routes-control extension)
    # =========================================================================

    def create_port_assoc_postcommit(self, context, port_assoc):
        """Apply port-specific EVPN configuration"""
        LOG.info("Creating port association for port %s", port_assoc['port_id'])
        # Port associations are primarily for route control
        # OVN BGP Agent will read port-level config if needed
        pass

    def update_port_assoc_postcommit(self, context, old_port_assoc,
                                     port_assoc):
        """Update port association"""
        LOG.info("Updating port association for port %s", port_assoc['port_id'])
        pass

    def delete_port_assoc_precommit(self, context, port_assoc):
        """Remove port-specific EVPN configuration"""
        LOG.info("Deleting port association for port %s", port_assoc['port_id'])
        pass

    def delete_port_assoc_postcommit(self, context, port_assoc):
        """Post-deletion cleanup"""
        pass

    # =========================================================================
    # Router Interface callbacks
    # =========================================================================

    @registry.receives(resources.ROUTER_INTERFACE, [events.AFTER_CREATE])
    @log_helpers.log_method_call
    def _on_router_interface_created(self, resource, event, trigger, payload):
        """Handle router interface addition"""
        try:
            context = payload.context
            router_id = payload.resource_id
            network_id = payload.metadata.get('port')['network_id']

            LOG.debug("Router interface created: router=%s, network=%s",
                      router_id, network_id)

            # Check if router has BGPVPN association
            bgpvpn = self._get_router_bgpvpn(context, router_id)
            if bgpvpn:
                self.ovn_client.update_logical_switch_evpn_config(
                    context, network_id, bgpvpn)

        except Exception as e:
            _log_callback_processing_exception(resource, event, trigger,
                                               payload.metadata, e)

    @registry.receives(resources.ROUTER_INTERFACE, [events.BEFORE_DELETE])
    @log_helpers.log_method_call
    def _on_router_interface_deleted(self, resource, event, trigger, payload):
        """Handle router interface removal"""
        try:
            context = payload.context
            router_id = payload.resource_id
            subnet_id = payload.metadata['subnet_id']

            # Get network for this subnet
            plugin = directory.get_plugin()
            network_id = plugin.get_subnet(context, subnet_id)['network_id']

            LOG.debug("Router interface deleted: router=%s, network=%s",
                      router_id, network_id)

            # Only clear if no other BGPVPN associations exist
            if not self._network_has_bgpvpn(context, network_id):
                self.ovn_client.clear_logical_switch_evpn_config(
                    context, network_id)

        except Exception as e:
            _log_callback_processing_exception(
                resource, event, trigger,
                {'subnet_id': subnet_id, 'router_id': router_id}, e)

    # =========================================================================
    # Helper methods
    # =========================================================================

    def _update_router_evpn_config(self, context, router_id, bgpvpn):
        """Apply EVPN config to all networks connected to router"""
        l3_plugin = directory.get_plugin(const.L3)
        router = l3_plugin.get_router(context, router_id)

        # Get all networks connected to this router
        plugin = directory.get_plugin()
        filters = {
            'device_id': [router_id],
            'device_owner': [const.DEVICE_OWNER_ROUTER_INTF]
        }
        router_ports = plugin.get_ports(context, filters=filters)

        for port in router_ports:
            network_id = port['network_id']
            self.ovn_client.update_logical_switch_evpn_config(
                context, network_id, bgpvpn)

    def _clear_router_evpn_config(self, context, router_id):
        """Remove EVPN config from router-connected networks"""
        plugin = directory.get_plugin()
        filters = {
            'device_id': [router_id],
            'device_owner': [const.DEVICE_OWNER_ROUTER_INTF]
        }
        router_ports = plugin.get_ports(context, filters=filters)

        for port in router_ports:
            network_id = port['network_id']
            # Only clear if no direct network association exists
            if not self._network_has_direct_bgpvpn(context, network_id):
                self.ovn_client.clear_logical_switch_evpn_config(
                    context, network_id)

    def _get_router_bgpvpn(self, context, router_id):
        """Get BGPVPN associated with router (if any)"""
        bgpvpns = self.get_bgpvpns(
            context,
            filters={'routers': [router_id]}
        )
        return bgpvpns[0] if bgpvpns else None

    def _network_has_direct_bgpvpn(self, context, network_id):
        """Check if network has direct BGPVPN association"""
        bgpvpns = self.get_bgpvpns(
            context,
            filters={'networks': [network_id]}
        )
        return bool(bgpvpns)

    def _network_has_router_bgpvpn(self, context, network_id):
        """Check if network is connected to a router with BGPVPN"""
        plugin = directory.get_plugin()
        filters = {
            'network_id': [network_id],
            'device_owner': [const.DEVICE_OWNER_ROUTER_INTF]
        }
        router_ports = plugin.get_ports(context, filters=filters)

        for port in router_ports:
            router_id = port['device_id']
            if self._get_router_bgpvpn(context, router_id):
                return True
        return False

    def _network_has_bgpvpn(self, context, network_id):
        """Check if network has any BGPVPN (direct or via router)"""
        return (self._network_has_direct_bgpvpn(context, network_id) or
                self._network_has_router_bgpvpn(context, network_id))