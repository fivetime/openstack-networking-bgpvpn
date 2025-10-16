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

"""OVN Database Client for BGPVPN

This module handles all interactions with the OVN Northbound and Southbound
databases, writing EVPN configuration into external_ids.
"""

import json

from neutron_lib.api.definitions import bgpvpn_routes_control as bgpvpn_rc_def
from neutron_lib.api.definitions import bgpvpn_vni as bgpvpn_vni_def
from neutron_lib.plugins import directory

from oslo_log import log as logging

from networking_bgpvpn.neutron.services.common import constants as svc_const
from networking_bgpvpn.neutron.services.common import ovn_utils

LOG = logging.getLogger(__name__)


class OVNClient:
    """Client for interacting with OVN databases"""

    def __init__(self):
        self._ovn_nb_idl = None
        self._ovn_sb_idl = None

    @property
    def _nb_idl(self):
        """Lazy initialization of OVN NB IDL connection"""
        if self._ovn_nb_idl is None:
            LOG.info("Initializing OVN NB IDL connection")

            # Get ML2 plugin
            plugin = directory.get_plugin()
            if not plugin:
                raise RuntimeError("Neutron core plugin not loaded")

            LOG.debug("Got plugin: %s", type(plugin))

            # Get OVN mechanism driver
            if not hasattr(plugin, 'mechanism_manager'):
                raise RuntimeError("Plugin does not have mechanism_manager")

            ovn_driver = None
            for name, driver in plugin.mechanism_manager.mech_drivers.items():
                LOG.debug("Checking mechanism driver: %s", name)
                # Check if this is OVN driver
                if hasattr(driver.obj, 'nb_ovn') and hasattr(driver.obj, 'sb_ovn'):
                    driver_class = driver.obj.__class__.__name__
                    if 'OVN' in driver_class:
                        ovn_driver = driver.obj
                        LOG.info("Found OVN mechanism driver: %s (class: %s)",
                                 name, driver_class)
                        break

            if not ovn_driver:
                raise RuntimeError(
                    "OVN mechanism driver not found. "
                    "Ensure ML2/OVN is configured in mechanism_drivers")

            # Use the driver's nb_ovn and sb_ovn IDL
            self._ovn_nb_idl = ovn_driver.nb_ovn
            self._ovn_sb_idl = ovn_driver.sb_ovn
            LOG.info("Successfully got OVN IDLs: NB=%s, SB=%s",
                     type(self._ovn_nb_idl), type(self._ovn_sb_idl))

        return self._ovn_nb_idl

    @property
    def _sb_idl(self):
        """Get OVN SB IDL (initializes NB if needed)"""
        if self._ovn_sb_idl is None:
            # Trigger NB initialization which also sets SB
            _ = self._nb_idl
        return self._ovn_sb_idl

    # =========================================================================
    # Logical_Switch operations (NB)
    # =========================================================================

    def _get_logical_switch(self, context, network_id):
        """Get OVN Logical_Switch for a Neutron network

        Args:
            context: Neutron context
            network_id: UUID of Neutron network

        Returns:
            OVN Logical_Switch row or None
        """
        ls_name = f"neutron-{network_id}"
        try:
            return self._nb_idl.ls_get(ls_name).execute(check_error=True)
        except Exception as e:
            LOG.warning("Failed to get Logical_Switch %s: %s", ls_name, e)
            return None

    def _build_evpn_external_ids(self, bgpvpn):
        """Build external_ids dictionary for EVPN configuration

        Args:
            bgpvpn: BGPVPN dict with configuration

        Returns:
            dict: external_ids to set on OVN resources
        """
        external_ids = {}

        # Required fields
        external_ids[svc_const.OVN_EVPN_TYPE_EXT_ID_KEY] = bgpvpn['type']
        external_ids[svc_const.OVN_EVPN_VNI_EXT_ID_KEY] = str(
            bgpvpn.get(bgpvpn_vni_def.VNI))

        # BGP AS (extracted from route targets)
        route_targets = bgpvpn.get('route_targets', [])
        if route_targets and ':' in route_targets[0]:
            bgp_as = route_targets[0].split(':')[0]
            external_ids[svc_const.OVN_EVPN_AS_EXT_ID_KEY] = bgp_as

        # Route targets (stored as JSON array)
        if route_targets:
            external_ids[svc_const.OVN_EVPN_RT_EXT_ID_KEY] = json.dumps(
                route_targets)

        import_targets = bgpvpn.get('import_targets', [])
        if import_targets:
            external_ids[svc_const.OVN_EVPN_IRT_EXT_ID_KEY] = json.dumps(
                import_targets)

        export_targets = bgpvpn.get('export_targets', [])
        if export_targets:
            external_ids[svc_const.OVN_EVPN_ERT_EXT_ID_KEY] = json.dumps(
                export_targets)

        # Route distinguishers
        route_distinguishers = bgpvpn.get('route_distinguishers', [])
        if route_distinguishers:
            external_ids[svc_const.OVN_EVPN_RD_EXT_ID_KEY] = json.dumps(
                route_distinguishers)

        # Local preference
        local_pref = bgpvpn.get(bgpvpn_rc_def.LOCAL_PREF_KEY)
        if local_pref is not None:
            external_ids[svc_const.OVN_EVPN_LOCAL_PREF_EXT_ID_KEY] = str(
                local_pref)

        LOG.debug("Built EVPN external_ids: %s", external_ids)
        return external_ids

    def update_logical_switch_evpn_config(self, context, network_id, bgpvpn):
        """Write EVPN configuration to OVN Logical_Switch

        Args:
            context: Neutron context
            network_id: UUID of Neutron network
            bgpvpn: BGPVPN dict with full configuration
        """
        ls = self._get_logical_switch(context, network_id)
        if not ls:
            LOG.error("Cannot update EVPN config: Logical_Switch not found "
                      "for network %s", network_id)
            return

        evpn_external_ids = self._build_evpn_external_ids(bgpvpn)

        LOG.info("Updating EVPN config for network %s (LS=%s): type=%s, vni=%s",
                 network_id, ls.name, bgpvpn['type'],
                 bgpvpn.get(bgpvpn_vni_def.VNI))

        try:
            with self._nb_idl.transaction(check_error=True) as txn:
                for key, value in evpn_external_ids.items():
                    txn.add(self._nb_idl.db_set(
                        'Logical_Switch', ls.uuid,
                        ('external_ids', {key: value})))

            LOG.info("Successfully updated EVPN config for network %s",
                     network_id)

        except Exception as e:
            LOG.error("Failed to update EVPN config for network %s: %s",
                      network_id, e)
            raise

    def clear_logical_switch_evpn_config(self, context, network_id):
        """Remove EVPN configuration from OVN Logical_Switch

        Args:
            context: Neutron context
            network_id: UUID of Neutron network
        """
        ls = self._get_logical_switch(context, network_id)
        if not ls:
            LOG.warning("Cannot clear EVPN config: Logical_Switch not found "
                        "for network %s", network_id)
            return

        LOG.info("Clearing EVPN config for network %s (LS=%s)",
                 network_id, ls.name)

        evpn_keys = ovn_utils.get_evpn_external_ids_keys()

        try:
            with self._nb_idl.transaction(check_error=True) as txn:
                for key in evpn_keys:
                    txn.add(self._nb_idl.db_remove(
                        'Logical_Switch', ls.uuid,
                        'external_ids', key, if_exists=True))

            LOG.info("Successfully cleared EVPN config for network %s",
                     network_id)

        except Exception as e:
            LOG.error("Failed to clear EVPN config for network %s: %s",
                      network_id, e)
            raise

    def get_logical_switch_evpn_config(self, context, network_id):
        """Read EVPN configuration from OVN Logical_Switch

        Args:
            context: Neutron context
            network_id: UUID of Neutron network

        Returns:
            dict: EVPN configuration or None if not configured
        """
        ls = self._get_logical_switch(context, network_id)
        if not ls:
            return None

        external_ids = ls.external_ids

        if svc_const.OVN_EVPN_VNI_EXT_ID_KEY not in external_ids:
            return None

        config = {
            'type': external_ids.get(
                svc_const.OVN_EVPN_TYPE_EXT_ID_KEY,
                svc_const.BGPVPN_L3),
            'vni': int(external_ids.get(svc_const.OVN_EVPN_VNI_EXT_ID_KEY, 0)),
        }

        # Parse JSON fields
        if svc_const.OVN_EVPN_RT_EXT_ID_KEY in external_ids:
            config['route_targets'] = json.loads(
                external_ids[svc_const.OVN_EVPN_RT_EXT_ID_KEY])

        if svc_const.OVN_EVPN_IRT_EXT_ID_KEY in external_ids:
            config['import_targets'] = json.loads(
                external_ids[svc_const.OVN_EVPN_IRT_EXT_ID_KEY])

        if svc_const.OVN_EVPN_ERT_EXT_ID_KEY in external_ids:
            config['export_targets'] = json.loads(
                external_ids[svc_const.OVN_EVPN_ERT_EXT_ID_KEY])

        if svc_const.OVN_EVPN_RD_EXT_ID_KEY in external_ids:
            config['route_distinguishers'] = json.loads(
                external_ids[svc_const.OVN_EVPN_RD_EXT_ID_KEY])

        if svc_const.OVN_EVPN_AS_EXT_ID_KEY in external_ids:
            config['bgp_as'] = external_ids[svc_const.OVN_EVPN_AS_EXT_ID_KEY]

        if svc_const.OVN_EVPN_LOCAL_PREF_EXT_ID_KEY in external_ids:
            config['local_pref'] = int(
                external_ids[svc_const.OVN_EVPN_LOCAL_PREF_EXT_ID_KEY])

        return config

    # =========================================================================
    # Port_Binding operations (SB)
    # =========================================================================

    def _get_port_binding(self, context, port_id):
        """Get OVN Port_Binding for a Neutron port

        Args:
            context: Neutron context
            port_id: UUID of Neutron port

        Returns:
            OVN Port_Binding row or None
        """
        # Router interface ports have 'lrp-' prefix
        logical_port = f"lrp-{port_id}"
        try:
            # Query SB database
            for pb in self._sb_idl.db_list_rows('Port_Binding').execute():
                if pb.logical_port == logical_port:
                    return pb
            LOG.warning("Port_Binding not found for port %s (lrp=%s)",
                        port_id, logical_port)
            return None
        except Exception as e:
            LOG.error("Failed to get Port_Binding for port %s: %s",
                      port_id, e)
            return None

    def update_port_binding_evpn_config(self, context, port_id, bgpvpn):
        """Write EVPN configuration to OVN Port_Binding external_ids

        The OVN BGP Agent monitors Port_Binding external_ids, particularly
        for router interface ports (lrp-*).

        Args:
            context: Neutron context
            port_id: UUID of Neutron port
            bgpvpn: BGPVPN dict with full configuration
        """
        pb = self._get_port_binding(context, port_id)
        if not pb:
            LOG.warning("Cannot update Port_Binding EVPN config: "
                        "Port_Binding not found for port %s", port_id)
            return

        evpn_external_ids = self._build_evpn_external_ids(bgpvpn)

        LOG.info("Updating Port_Binding EVPN config for port %s (logical_port=%s)",
                 port_id, pb.logical_port)

        try:
            with self._sb_idl.transaction(check_error=True) as txn:
                for key, value in evpn_external_ids.items():
                    txn.add(self._sb_idl.db_set(
                        'Port_Binding', pb.uuid,
                        ('external_ids', {key: value})))

            LOG.info("Successfully updated Port_Binding EVPN config for port %s",
                     port_id)

        except Exception as e:
            LOG.error("Failed to update Port_Binding EVPN config for port %s: %s",
                      port_id, e)
            raise

    def clear_port_binding_evpn_config(self, context, port_id):
        """Remove EVPN configuration from OVN Port_Binding

        Args:
            context: Neutron context
            port_id: UUID of Neutron port
        """
        pb = self._get_port_binding(context, port_id)
        if not pb:
            LOG.warning("Cannot clear Port_Binding EVPN config: "
                        "Port_Binding not found for port %s", port_id)
            return

        LOG.info("Clearing Port_Binding EVPN config for port %s (logical_port=%s)",
                 port_id, pb.logical_port)

        evpn_keys = ovn_utils.get_evpn_external_ids_keys()

        try:
            with self._sb_idl.transaction(check_error=True) as txn:
                for key in evpn_keys:
                    txn.add(self._sb_idl.db_remove(
                        'Port_Binding', pb.uuid,
                        'external_ids', key, if_exists=True))

            LOG.info("Successfully cleared Port_Binding EVPN config for port %s",
                     port_id)

        except Exception as e:
            LOG.error("Failed to clear Port_Binding EVPN config for port %s: %s",
                      port_id, e)
            raise