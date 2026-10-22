// SPDX-License-Identifier: MIT
// One emulated BGP router: a single session to the DUT (RFC 4271 FSM, active
// side), paced announcements, and a receive store with arrival timestamps.
// A Router lives on exactly one asio::io_context thread - no locking.
#pragma once

#include <array>
#include <bitset>
#include <list>
#include <map>
#include <cstdint>
#include <random>
#include <deque>
#include <functional>
#include <memory>
#include <string>
#include <unordered_map>
#include <unordered_set>
#include <vector>

#include <asio.hpp>
#include <nlohmann/json.hpp>

#include "tgen/bgp.hpp"
#include "tgen/plan.hpp"

namespace tgen {

std::int64_t now_ns();  // CLOCK_REALTIME in ns (chrony-synchronised across the cluster)
inline std::int64_t ms_to_ns(std::int64_t ms) { return ms * 1'000'000; }

enum class State { Idle, Connect, OpenSent, OpenConfirm, Established, Stopped };
const char* state_name(State s);

// Phases (docs/07-agent-api.md). A Router executes them on its own io thread.
enum class PhaseType { Establish, Announce, Steady, Churn, Flap, Withdraw, Teardown, End };
enum class FlapKind { Reset, Silent, WithdrawAll };

struct PhaseCmd {
  PhaseType type = PhaseType::Steady;
  std::int64_t at_ns = 0;
  double rate = 0;                       // announce / withdraw / teardown: prefixes/s; churn: operations/s
  double mix_announce = 40, mix_withdraw = 40, mix_attr = 20;   // churn, percent
  FlapKind flap = FlapKind::Reset;
  double down_s = 5;                     // flap
  bool close = false;                    // teardown: close sessions right after our own withdrawals
  std::int64_t offset_ns = 0;            // establish: per-router stagger, set by the Run
};

// Generation tag for churn announcements: large community (router ASN, kGenTag, generation)
constexpr std::uint32_t kGenTag = 0x7467;  // "tg"

struct LiveSample { std::uint64_t key; std::uint32_t gen; std::uint8_t kind; std::int64_t t; };  // kind 1 ann, 2 wd, 3 attr

struct LiveSnapshot {
  std::string name;
  State state = State::Idle;
  std::uint64_t held = 0, nlri_rx = 0, withdrawn_rx = 0, prefixes_sent = 0, prefixes_withdrawn = 0,
                updates_sent = 0, updates_rx = 0, looped_rx = 0,
                bytes_sent = 0, bytes_rx = 0;
  int resets = 0, planned_resets = 0;
  bool converged = false;
  double send_lag_p99_ms = 0;                         // over the sends since the previous snapshot
  std::vector<LiveSample> tx, rx;                      // live-sampled, since the previous snapshot
};

struct RouterStats {
  State state = State::Idle;
  int connect_attempts = 0;
  int sessions_established = 0;
  int resets = 0;                     // Established -> down, not planned
  int planned_resets = 0;             // Established -> down caused by a flap phase
  std::int64_t t_connect_start = 0;   // TCP connect start of the first successful session
  std::int64_t t_established = 0;     // first time Established
  std::uint32_t peer_asn = 0, peer_router_id = 0;
  std::uint16_t hold_negotiated = 0;
  bool as4 = false;
  // send
  std::uint64_t updates_sent = 0, prefixes_sent = 0, withdraw_updates_sent = 0, prefixes_withdrawn = 0,
                bytes_sent = 0;
  std::int64_t t_first_send = 0, t_last_send = 0;          // announcements
  std::int64_t t_first_wd_send = 0, t_last_wd_send = 0;    // withdrawals
  std::vector<std::int64_t> send_lag_ns;  // paced mode: write completion - scheduled release
  // receive
  std::uint64_t updates_rx = 0, nlri_rx = 0, withdrawn_rx = 0, keepalives_rx = 0, end_of_rib_rx = 0;
  std::uint64_t routes_held = 0, routes_peak = 0;
  std::uint64_t bytes_rx = 0, bytes_rx_at_converged = 0;  // BGP bytes from the DUT (TCP payload)
  std::int64_t t_first_rx = 0, t_converged = 0, t_last_rx = 0, t_empty_after_withdraw = 0;
  std::uint64_t paths_with_dut_asn = 0, next_hop_is_dut = 0, paths_without_originator = 0;
  std::map<int, std::uint64_t> attr_rx;  // attribute type code -> NLRI received with it
  std::uint64_t looped_rx = 0;  // own routes sent back (own AS in AS_PATH / own ORIGINATOR_ID): dropped, RFC 4271 9.1.2

  std::int64_t max_keepalive_gap_ns = 0;
  std::vector<std::pair<std::uint8_t, std::uint8_t>> notifications_rx, notifications_tx;
  std::vector<std::string> errors;
  // churn / flap
  std::uint64_t churn_ann = 0, churn_wd = 0, churn_attr = 0, churn_updates = 0;
  std::vector<std::pair<std::string, std::int64_t>> flaps;   // kind, time
};

class Router : public std::enable_shared_from_this<Router> {
 public:
  Router(asio::io_context& io, const Plan& plan, const RouterPlan& rp, std::int64_t establish_offset_ns);
  void start();  // CLI mode: schedules the whole timeline from plan.timing
  void schedule(const PhaseCmd& cmd);   // serve mode: one phase at cmd.at_ns (call on the router's io thread)
  void stop_now();                      // Cease and close now (call on the router's io thread)
  bool stopped() const { return st_.state == State::Stopped; }
  const std::string& name() const { return rp_.name; }
  LiveSnapshot snapshot();             // call on the router's io thread; hands over the live samples
  nlohmann::json result() const;

 private:
  enum class Kind { Control, Announce, Withdraw };
  struct Out {
    std::shared_ptr<bgp::Bytes> msg;
    Kind kind = Kind::Control;
    std::size_t prefixes = 0;
    std::vector<bgp::Prefix> samples;  // sampled prefixes inside this UPDATE
    std::int64_t scheduled = 0;        // paced release time; 0 = not paced
    std::uint32_t gen = 0;             // churn generation (0 = initial load)
    int churn_kind = 0;                // 0 = not churn; 1 announce, 2 withdraw, 3 attribute change
  };
  static Out control(bgp::Bytes m) {
    Out o;
    o.msg = std::make_shared<bgp::Bytes>(std::move(m));
    return o;
  }
  struct Batch {                       // pre-encoded UPDATE + metadata
    std::shared_ptr<bgp::Bytes> msg;
    std::size_t prefixes = 0;
    std::vector<bgp::Prefix> samples;
  };

  void at(std::int64_t epoch_ns, std::function<void()> fn, asio::steady_timer& t);
  void connect();
  void on_connected(std::uint64_t gen, const std::error_code& ec);
  void read_loop();
  void handle(const bgp::Message& m);
  void on_open(const bgp::OpenMsg& o);
  void on_update(const bgp::UpdateMsg& u);
  void after_rx(std::int64_t t, bool keep);  // held/peak/convergence bookkeeping
  void established();
  void build_batches(bool withdraw, const std::vector<std::size_t>& list);
  void begin(bool withdraw, double rate, const std::vector<std::size_t>& list);
  std::vector<std::size_t> idx_where(std::uint8_t v) const;
  void stop_churn();
  void run_phase(const PhaseCmd& cmd);
  void churn_tick();
  void send_churn(std::vector<std::size_t>& idx, int kind);
  void mark_all(bool announced);
  void record_tx(const std::vector<bgp::Prefix>& samples, std::uint32_t gen, int kind, std::int64_t t);
  void pump();
  void send(Out o);
  void write_next();
  void arm_hold(int seconds);
  void arm_keepalive();
  void fail(std::uint8_t code, std::uint8_t subcode, const std::string& why);
  void down(const std::string& why);
  void stop_all();
  void log(const std::string& msg) const;

  asio::io_context& io_;
  const Plan& plan_;
  const RouterPlan& rp_;
  asio::ip::tcp::socket sock_;
  asio::steady_timer t_establish_, t_announce_, t_withdraw_, t_end_, t_hold_, t_keepalive_, t_retry_, t_pace_,
      t_churn_, t_flap_;
  std::list<asio::steady_timer> phase_timers_;  // serve mode: scheduled phases
  std::array<std::uint8_t, 65536> rxbuf_{};
  bgp::StreamParser parser_;
  std::uint64_t gen_ = 0;              // connection generation; stale callbacks are ignored
  std::int64_t rx_time_ = 0;           // arrival time of the current read
  std::int64_t establish_offset_ns_ = 0;
  std::uint32_t local_as_ = 0;
  std::bitset<256> sent_types_;  // attribute types in our own announcements

  std::deque<Out> outq_;
  bool writing_ = false;

  std::vector<Batch> cur_;             // UPDATEs of the current announce / withdraw phase
  std::size_t next_batch_ = 0;
  bool active_ = false, withdrawing_ = false, stopping_ = false;
  bool announce_due_ = false, withdraw_due_ = false;
  std::int64_t pace_start_ = 0;
  double pace_prefixes_ = 0;           // cumulative prefixes released in this phase
  double rate_ = 0;                    // current announce/withdraw rate (prefixes/s, 0 = max)
  std::vector<std::uint8_t> announced_;  // per prefix: currently announced by us
  // churn
  bool churning_ = false, churn_seen_ = false;
  PhaseCmd churn_;
  std::int64_t churn_last_ = 0;
  double churn_due_ = 0;
  std::uint32_t gen_counter_ = 0;
  std::mt19937_64 rng_;
  std::vector<std::size_t> ann_idx_, wd_idx_;   // indices of announced / withdrawn prefixes (churn)
  std::vector<std::size_t> pos_;                // index -> position in ann_idx_ or wd_idx_
  std::vector<std::array<std::int64_t, 4>> churn_tx_;   // key, gen, kind, t (sampled)
  std::vector<std::array<std::int64_t, 3>> churn_rx_;   // key, gen, t (sampled, every arrival)
  std::vector<std::array<std::int64_t, 2>> churn_wd_rx_;  // key, t
  // flap
  bool planned_down_ = false, silent_ = false, close_after_withdraw_ = false;
  double reconnect_after_s_ = 0;
  int connect_failures_ = 0;
  // live view
  std::vector<LiveSample> live_tx_, live_rx_;
  std::size_t lag_mark_ = 0;

  std::unordered_set<std::uint64_t> held_;
  std::unordered_map<std::uint64_t, std::int64_t> sample_tx_, sample_rx_, sample_wd_tx_, sample_wd_rx_;
  std::int64_t last_keepalive_rx_ = 0;
  RouterStats st_;
};

}  // namespace tgen
