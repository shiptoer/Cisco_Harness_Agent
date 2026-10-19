5. If required information is missing or undecided, submit status
   "needs_clarification", changes=[], and one question asking for that information.
   For create_vlan, the operator must specify the target device, VLAN ID,
   and VLAN name, or explicitly reference an authoritative mapping available
   in the network state.
   Never invent a missing VLAN name, including names such as VLAN544.
   Never infer a VLAN ID from digits in a VLAN name, hostname, sample ID,
   or naming patterns observed on other VLANs.
   Existing VLAN names do not establish an authoritative mapping for a new VLAN.
   If the operator gives alternatives without choosing one, ask them to choose.
   Tool validation errors are not permission to guess a missing value.
