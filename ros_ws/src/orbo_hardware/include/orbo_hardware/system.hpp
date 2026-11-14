#pragma once
#include <array>
#include <atomic>
#include <chrono>
#include <mutex>
#include <thread>
#include "hardware_interface/system_interface.hpp"
#include "rclcpp/rclcpp.hpp"
#include "orbo_embedded_interfaces/msg/wheel_state.hpp"
#include "orbo_embedded_interfaces/msg/wheel_command.hpp"
#include "orbo_embedded_interfaces/msg/control_session.hpp"

namespace orbo_hardware {
class System : public hardware_interface::SystemInterface {
 public:
  ~System() override;
  hardware_interface::CallbackReturn on_init(const hardware_interface::HardwareInfo &) override;
  hardware_interface::CallbackReturn on_configure(const rclcpp_lifecycle::State &) override;
  hardware_interface::CallbackReturn on_activate(const rclcpp_lifecycle::State &) override;
  hardware_interface::CallbackReturn on_deactivate(const rclcpp_lifecycle::State &) override;
  hardware_interface::CallbackReturn on_cleanup(const rclcpp_lifecycle::State &) override;
  hardware_interface::CallbackReturn on_error(const rclcpp_lifecycle::State &) override;
  std::vector<hardware_interface::StateInterface> export_state_interfaces() override;
  std::vector<hardware_interface::CommandInterface> export_command_interfaces() override;
  hardware_interface::return_type read(const rclcpp::Time &, const rclcpp::Duration &) override;
  hardware_interface::return_type write(const rclcpp::Time &, const rclcpp::Duration &) override;
 private:
  using Steady = std::chrono::steady_clock;
  void worker();
  void publish_tick();
  void stop_worker();
  void stop_request();
  bool fresh_locked(Steady::time_point) const;
  std::array<double,2> position_{},velocity_{},command_{};
  std::array<double,2> position_origin_{};
  bool have_position_origin_=false;
  std::array<std::atomic<double>,2> queued_{};
  std::atomic<int64_t> last_write_ns_{0};
  std::atomic<bool> running_{false},active_{false},fault_{false},request_stop_{false};
  double max_velocity_=0;
  uint64_t boot_=0,sequence_=0;
  std::mutex mutex_;
  orbo_embedded_interfaces::msg::WheelState latest_;
  orbo_embedded_interfaces::msg::ControlSession authority_;
  Steady::time_point received_{}, authority_received_{}, last_publication_{};
  uint64_t last_source_ms_=0;
  rclcpp::Node::SharedPtr node_;
  std::shared_ptr<rclcpp::executors::SingleThreadedExecutor> executor_;
  rclcpp::Subscription<orbo_embedded_interfaces::msg::WheelState>::SharedPtr state_sub_;
  rclcpp::Subscription<orbo_embedded_interfaces::msg::ControlSession>::SharedPtr authority_sub_;
  rclcpp::Publisher<orbo_embedded_interfaces::msg::WheelCommand>::SharedPtr wheel_pub_;
  rclcpp::Publisher<orbo_embedded_interfaces::msg::ControlSession>::SharedPtr session_pub_;
  rclcpp::TimerBase::SharedPtr publish_timer_;
  std::thread thread_;
};
}
