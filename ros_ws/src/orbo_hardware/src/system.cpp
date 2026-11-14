#include "orbo_hardware/system.hpp"
#include "pluginlib/class_list_macros.hpp"
#include <cmath>
#include <limits>

namespace orbo_hardware {
using hardware_interface::CallbackReturn;
using hardware_interface::return_type;
System::~System(){stop_worker();}
CallbackReturn System::on_init(const hardware_interface::HardwareInfo &info){
  if(hardware_interface::SystemInterface::on_init(info)!=CallbackReturn::SUCCESS)return CallbackReturn::ERROR;
  if(info_.joints.size()!=2)return CallbackReturn::ERROR;
  for(const auto &joint:info_.joints){
    if(joint.command_interfaces.size()!=1 || joint.command_interfaces[0].name!="velocity" ||
       joint.state_interfaces.size()!=2 || joint.state_interfaces[0].name!="position" || joint.state_interfaces[1].name!="velocity")return CallbackReturn::ERROR;
  }
  try{
    if(info_.hardware_parameters.at("calibrated")!="true")return CallbackReturn::ERROR;
    max_velocity_=std::stod(info_.hardware_parameters.at("max_velocity_rad_s"));
    if(!std::isfinite(max_velocity_) || max_velocity_<=0 || max_velocity_>6)return CallbackReturn::ERROR;
  }catch(const std::exception &){return CallbackReturn::ERROR;}
  position_.fill(std::numeric_limits<double>::quiet_NaN());
  velocity_.fill(std::numeric_limits<double>::quiet_NaN());
  return CallbackReturn::SUCCESS;
}
std::vector<hardware_interface::StateInterface> System::export_state_interfaces(){
  std::vector<hardware_interface::StateInterface> result;
  for(size_t i=0;i<2;i++){
    result.emplace_back(info_.joints[i].name,"position",&position_[i]);
    result.emplace_back(info_.joints[i].name,"velocity",&velocity_[i]);
  }return result;
}
std::vector<hardware_interface::CommandInterface> System::export_command_interfaces(){
  std::vector<hardware_interface::CommandInterface> result;
  for(size_t i=0;i<2;i++)result.emplace_back(info_.joints[i].name,"velocity",&command_[i]);
  return result;
}
CallbackReturn System::on_configure(const rclcpp_lifecycle::State &){
  stop_worker();fault_=false;boot_=0;sequence_=0;received_={};authority_received_={};
  authority_=orbo_embedded_interfaces::msg::ControlSession();have_position_origin_=false;
  last_publication_={};last_source_ms_=0;
  node_=std::make_shared<rclcpp::Node>("orbo_hardware_transport","/orbo");
  executor_=std::make_shared<rclcpp::executors::SingleThreadedExecutor>();
  executor_->add_node(node_);
  state_sub_=node_->create_subscription<orbo_embedded_interfaces::msg::WheelState>("embedded/wheel_state",rclcpp::SensorDataQoS().keep_last(1),
    [this](orbo_embedded_interfaces::msg::WheelState::ConstSharedPtr msg){
      std::lock_guard<std::mutex> guard(mutex_);
      if(boot_ && boot_!=msg->boot_id){fault_=true;request_stop_=true;return;}
      if(received_.time_since_epoch().count() && msg->sequence<=latest_.sequence)return;
      latest_=*msg;boot_=msg->boot_id;received_=Steady::now();
    });
  // Only the local broker velocity gate publishes this authorization topic.
  // It preserves the originating TwistStamped age; health never creates authority.
  authority_sub_=node_->create_subscription<orbo_embedded_interfaces::msg::ControlSession>("control/authority",rclcpp::QoS(1).durability_volatile(),
    [this](orbo_embedded_interfaces::msg::ControlSession::ConstSharedPtr msg){
      {
        std::lock_guard<std::mutex> guard(mutex_);
        if(msg->interface_version!=1 || !msg->enable_wheels || msg->valid_for_ms>500 || msg->valid_for_ms==0)return;
        if(authority_.epoch>msg->epoch || (authority_.epoch==msg->epoch && authority_.sequence>=msg->sequence))return;
        authority_=*msg;authority_received_=Steady::now();
      }
      publish_tick();
    });
  wheel_pub_=node_->create_publisher<orbo_embedded_interfaces::msg::WheelCommand>("embedded/wheel_command",rclcpp::QoS(1).durability_volatile());
  session_pub_=node_->create_publisher<orbo_embedded_interfaces::msg::ControlSession>("embedded/control_session",rclcpp::QoS(1).durability_volatile());
  // Receive continuously and dispatch fresh intent immediately when the 20 Hz
  // rate limit allows. The timer releases pending intent without resending old stamps.
  publish_timer_=node_->create_wall_timer(std::chrono::milliseconds(5), [this]{publish_tick();});
  running_=true;thread_=std::thread(&System::worker,this);
  return CallbackReturn::SUCCESS;
}
bool System::fresh_locked(Steady::time_point now) const{
  if(!received_.time_since_epoch().count() || now-received_>std::chrono::milliseconds(150))return false;
  for(size_t i=0;i<2;i++)if(!latest_.valid[i] || latest_.hardware_error[i] ||
    !std::isfinite(latest_.position_rad[i]) || !std::isfinite(latest_.velocity_rad_s[i]) ||
    latest_.sample_mono_us<latest_.motor_sample_mono_us[i] ||
    latest_.sample_mono_us-latest_.motor_sample_mono_us[i]>150000)return false;
  const auto a=latest_.motor_sample_mono_us[0],b=latest_.motor_sample_mono_us[1];
  return (a>b?a-b:b-a)<100000;
}
CallbackReturn System::on_activate(const rclcpp_lifecycle::State &){
  std::lock_guard<std::mutex> guard(mutex_);
  if(fault_ || !fresh_locked(Steady::now()))return CallbackReturn::ERROR;
  command_.fill(0);for(auto &v:queued_)v=0;last_write_ns_=0;
  active_=true;return CallbackReturn::SUCCESS; // Activation does not arm the MCU.
}
void System::stop_request(){active_=false;request_stop_=true;for(auto &v:queued_)v=0;}
CallbackReturn System::on_deactivate(const rclcpp_lifecycle::State &){stop_request();return CallbackReturn::SUCCESS;}
CallbackReturn System::on_error(const rclcpp_lifecycle::State &){fault_=true;stop_request();return CallbackReturn::SUCCESS;}
CallbackReturn System::on_cleanup(const rclcpp_lifecycle::State &){stop_worker();return CallbackReturn::SUCCESS;}
return_type System::read(const rclcpp::Time &,const rclcpp::Duration &){
  std::unique_lock<std::mutex> guard(mutex_,std::try_to_lock);
  if(!guard.owns_lock())return fault_?return_type::ERROR:return_type::OK;
  if(fault_)return return_type::ERROR;
  if(!fresh_locked(Steady::now())){if(active_){fault_=true;stop_request();return return_type::ERROR;}return return_type::OK;}
  // Absolute motor counts may include earlier sessions. Establish a measured
  // origin once per configuration so first odometry is not historical travel.
  if(!have_position_origin_){
    for(size_t i=0;i<2;i++)position_origin_[i]=latest_.position_rad[i];
    have_position_origin_=true;
  }
  for(size_t i=0;i<2;i++){position_[i]=latest_.position_rad[i]-position_origin_[i];velocity_[i]=latest_.velocity_rad_s[i];}
  return return_type::OK;
}
return_type System::write(const rclcpp::Time &,const rclcpp::Duration &){
  if(fault_)return return_type::ERROR;
  if(!active_)return return_type::OK;
  for(size_t i=0;i<2;i++)if(!std::isfinite(command_[i]) || std::abs(command_[i])>max_velocity_){fault_=true;stop_request();return return_type::ERROR;}
  for(size_t i=0;i<2;i++)queued_[i]=command_[i];
  last_write_ns_=std::chrono::duration_cast<std::chrono::nanoseconds>(Steady::now().time_since_epoch()).count();
  return return_type::OK;
}
void System::publish_tick(){
    if(request_stop_.exchange(false)){
      orbo_embedded_interfaces::msg::ControlSession stop;
      stop.interface_version=1;stop.operation=0;session_pub_->publish(stop);
    }
    auto now=Steady::now();
    if(active_ && !fault_){
      std::lock_guard<std::mutex> guard(mutex_);
      if(!fresh_locked(now)){fault_=true;stop_request();}
      else{
        auto now_ns=std::chrono::duration_cast<std::chrono::nanoseconds>(now.time_since_epoch()).count();
        uint64_t epoch_ms=static_cast<uint64_t>(node_->now().nanoseconds()/1000000);
        bool valid=authority_.enable_wheels && authority_.boot_id==boot_ && authority_.epoch==latest_.epoch && latest_.armed &&
          epoch_ms>=authority_.issued_epoch_ms && epoch_ms-authority_.issued_epoch_ms<authority_.valid_for_ms &&
          now-authority_received_<std::chrono::milliseconds(authority_.valid_for_ms) &&
          last_write_ns_>0 && now_ns-last_write_ns_<100000000;
        if(valid && authority_.issued_epoch_ms!=last_source_ms_ &&
           now-last_publication_>=std::chrono::milliseconds(50)){
          orbo_embedded_interfaces::msg::WheelCommand msg;
          msg.boot_id=boot_;msg.epoch=authority_.epoch;msg.sequence=++sequence_;msg.command_id=sequence_;
          msg.issued_epoch_ms=authority_.issued_epoch_ms; // Preserve source age, never refresh old intent.
          msg.valid_for_ms=authority_.valid_for_ms;
          for(size_t i=0;i<2;i++)msg.velocity_rad_s[i]=queued_[i].load();
          last_source_ms_=msg.issued_epoch_ms;last_publication_=now;
          wheel_pub_->publish(msg);
        }
        // Loss of upstream authority stops refresh; the MCU expiry independently disarms.
      }
    }
}
void System::worker(){
  while(running_ && rclcpp::ok())executor_->spin_once(std::chrono::milliseconds(10));
  if(session_pub_){orbo_embedded_interfaces::msg::ControlSession stop;stop.interface_version=1;stop.operation=0;session_pub_->publish(stop);}
}
void System::stop_worker(){
  active_=false;running_=false;
  if(executor_)executor_->cancel();
  if(thread_.joinable())thread_.join();
  publish_timer_.reset();
  state_sub_.reset();authority_sub_.reset();wheel_pub_.reset();session_pub_.reset();executor_.reset();node_.reset();
}
}
PLUGINLIB_EXPORT_CLASS(orbo_hardware::System,hardware_interface::SystemInterface)
