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

"""Common utilities for OVN BGPVPN integration

This module provides utilities that can be used by any OVN-related
BGPVPN components, not specific to the OVN driver.
"""

from oslo_log import log as logging

from networking_bgpvpn.neutron.services.common import constants

LOG = logging.getLogger(__name__)


def verify_ovn_bgp_agent_compatibility():
    """Verify OVN BGP Agent is properly configured

    This is a deployment-time check, not driver-specific.
    Can be called by CLI tools or health checks.

    Returns:
        tuple: (bool, str) - (is_compatible, message)
    """
    recommendations = get_recommended_agent_config()
    LOG.info("OVN BGPVPN integration requires OVN BGP Agent with "
             "driver=ovn_evpn_driver. See documentation for details.")
    return True, recommendations


def get_recommended_agent_config():
    """Return recommended OVN BGP Agent configuration

    This is generic advice, not driver-specific.
    """
    return """
# Recommended ovn-bgp-agent.conf for BGPVPN integration

[DEFAULT]
driver = ovn_evpn_driver
exposing_method = vrf
evpn_local_ip = <VTEP_IP>
bgp_AS = <YOUR_AS>
bgp_router_id = <ROUTER_ID>

[ovn]
ovn_nb_connection = tcp:<NB_IP>:6641
ovn_sb_connection = tcp:<SB_IP>:6642
"""


def get_evpn_external_ids_keys():
    """Get list of all EVPN-related external_ids keys

    Useful for cleanup or migration operations.

    Returns:
        list: All OVN external_ids keys used for EVPN configuration
    """
    return [
        constants.OVN_EVPN_TYPE_EXT_ID_KEY,
        constants.OVN_EVPN_VNI_EXT_ID_KEY,
        constants.OVN_EVPN_AS_EXT_ID_KEY,
        constants.OVN_EVPN_RT_EXT_ID_KEY,
        constants.OVN_EVPN_IRT_EXT_ID_KEY,
        constants.OVN_EVPN_ERT_EXT_ID_KEY,
        constants.OVN_EVPN_RD_EXT_ID_KEY,
        constants.OVN_EVPN_LOCAL_PREF_EXT_ID_KEY,
    ]