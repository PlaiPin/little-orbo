uint64 boot_id
uint64 epoch
uint64 command_id
uint64 sequence
uint64 mono_us
uint8 state
uint16 reason
uint8 evidence
# state: 1 accepted, 2 executing, 3 controller_finished, 4 rejected, 5 stopped, 6 fault
# evidence: 0 none, 1 controller_ack; never independent observed success
