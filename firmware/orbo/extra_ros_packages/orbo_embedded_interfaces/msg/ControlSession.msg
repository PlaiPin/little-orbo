uint32 interface_version
uint64 boot_id
uint64 epoch
uint64 sequence
uint64 issued_epoch_ms
uint32 valid_for_ms
uint8 operation
bool enable_wheels
# operation: 0 stop, 1 arm; arm must increase epoch, and never auto-rearms
