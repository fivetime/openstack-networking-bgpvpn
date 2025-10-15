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

"""OVN NB Database Client for BGPVPN

This module handles all interactions with the OVN Northbound database,
writing EVPN configuration into Logical_Switch external_ids.
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
    """Client for interacting with OVN Northbound database"""

    def __init__(self):
        self._ovn_nb_idl = None

    @property
    def _nb_idl(self):
        """Lazy initialization of OVN NB IDL connection"""
        if self._ovn_nb_idl is None:
            # Get the OVN NB IDL from the ML2/OVN mechanism driver
            try:
                from neutron.plugins.ml2.drivers.ovn.mech_driver.ovsdb \
                    import impl_idl_ovn
                # Get the singleton instance
                self._ovn_nb_idl = impl_idl_ovn.OvnNbIdlForLb.get_instance()
            except ImportError:
                # Fallback: try to get from the core plugin
                plugin = directory.get_plugin()
                if hasattr(plugin, '_nb_ovn'):
                    self._ovn_nb_idl = plugin._nb_ovn
                else:
                    raise RuntimeError(
                        "Cannot find OVN NB IDL connection. "
                        "Ensure ML2/OVN plugin is loaded.")
        return self._ovn_nb_idl

    def _get_logical_switch(self, context, network_id):
        """Get OVN Logical_Switch for a Neutron network

        Args:
            context: Neutron context
            network_id: UUID of Neutron network

        Returns:
            OVN Logical_Switch row or None
        """
        # OVN uses 'neutron-<network_id>' as Logical_Switch name
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
            dict: external_ids to set on Logical_Switch
        """
        external_ids = {}

        # Required fields
        external_ids[svc_const.OVN_EVPN_TYPE_EXT_ID_KEY] = bgpvpn['type']
        external_ids[svc_const.OVN_EVPN_VNI_EXT_ID_KEY] = str(
            bgpvpn.get(bgpvpn_vni_def.VNI))

        # Optional: BGP AS (can be derived from route targets if not set)
        # We extract AS from the first route target (format: AS:NN)
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

        # Local preference (for routes-control extension)
        local_pref = bgpvpn.get(bgpvpn_rc_def.LOCAL_PREF_KEY)
        if local_pref is not None:
            external_ids[svc_const.OVN_EVPN_LOCAL_PREF_EXT_ID_KEY] = str(
                local_pref)

        LOG.debug("Built EVPN external_ids: %s", external_ids)
        return external_ids

    def update_logical_switch_evpn_config(self, context, network_id, bgpvpn):
        """Write EVPN configuration to OVN Logical_Switch

        This method updates the external_ids of the Logical_Switch corresponding
        to the given network with EVPN parameters. The OVN BGP Agent monitors
        these fields and configures the data plane accordingly.

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
            # Merge with existing external_ids
            with self._nb_idl.transaction(check_error=True) as txn:
                # Update each key individually to preserve other external_ids
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

        # Get all EVPN keys from utility function
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

        # Check if EVPN is configured
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