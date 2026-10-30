// SPDX-License-Identifier: MIT
// One run on one node: the plan, the io threads and the emulated routers.
// Routers live on their io thread; everything that touches them from outside
// (phases, stats, results, stop) is posted to that thread.
#pragma once

#include <cstdint>
#include <functional>
#include <memory>
#include <string>
#include <thread>
#include <vector>

#include <asio.hpp>
#include <nlohmann/json.hpp>

#include "tgen/plan.hpp"
#include "tgen/router.hpp"

namespace tgen {

PhaseCmd phase_from_json(const nlohmann::json& j);   // throws std::invalid_argument

class Run {
 public:
  explicit Run(Plan plan, std::string version);
  ~Run();
  Run(const Run&) = delete;
  Run& operator=(const Run&) = delete;

  const Plan& plan() const { return plan_; }
  void start_cli();                         // CLI mode: whole timeline from plan.timing
  void schedule(const PhaseCmd& cmd);       // serve mode
  void schedule_for(const PhaseCmd& cmd, const std::vector<std::string>& names);  // flap: named routers only
  void stop();                              // Cease and close on every router; waits up to 3 s
  bool all_stopped();
  nlohmann::json stats();                   // live counters + live samples since the previous call
  nlohmann::json result();                  // full result (same shape as the CLI result file)
  std::size_t routers() const { return routers_.size(); }

 private:
  void on_each_io(std::function<void(Router&)> fn);  // fn(router) on each router's thread, waits for all

  Plan plan_;
  std::string version_;
  std::vector<std::unique_ptr<asio::io_context>> ios_;
  std::vector<asio::executor_work_guard<asio::io_context::executor_type>> guards_;
  std::vector<std::thread> threads_;
  std::vector<std::shared_ptr<Router>> routers_;
  std::uint64_t seq_ = 0;
  std::uint64_t cpu_prev_total_ = 0, cpu_prev_idle_ = 0;   // /proc/stat at the previous stats() call
  std::int64_t t_start_ = 0;
};

}  // namespace tgen
