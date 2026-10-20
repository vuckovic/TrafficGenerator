// SPDX-License-Identifier: MIT
#include "tgen/router.hpp"

#include <algorithm>
#include <chrono>
#include <cstdio>
#include <ctime>

namespace tgen {

using asio::ip::tcp;
using nlohmann::json;

std::int64_t now_ns() {
  return std::chrono::duration_cast<std::chrono::nanoseconds>(std::chrono::system_clock::now().time_since_epoch())
      .count();
}

const char* state_name(State s) {
  switch (s) {
    case State::Idle: return "Idle";
    case State::Connect: return "Connect";
    case State::OpenSent: return "OpenSent";
    case State::OpenConfirm: return "OpenConfirm";
    case State::Established: return "Established";
    case State::Stopped: return "Stopped";
  }
  return "?";
}

Router::Router(asio::io_context& io, const Plan& plan, const RouterPlan& rp, std::int64_t establish_offset_ns)
    : io_(io), plan_(plan), rp_(rp), sock_(io), t_establish_(io), t_announce_(io), t_withdraw_(io), t_end_(io),
      t_hold_(io), t_keepalive_(io), t_retry_(io), t_pace_(io), establish_offset_ns_(establish_offset_ns) {
  // eBGP to a route server or router: own ASN. iBGP to a route reflector: DUT's AS.
  local_as_ = plan.role == DutRole::RouteReflector ? plan.dut_asn : rp.asn;
  if (rp.receive != ReceiveMode::Count && rp.expected_rx) held_.reserve(rp.expected_rx + rp.expected_rx / 8);
}

void Router::log(const std::string& msg) const {
  const auto t = now_ns();
  std::time_t s = t / 1'000'000'000;
  std::tm tm{};
  gmtime_r(&s, &tm);
  std::fprintf(stderr, "%02d:%02d:%02d.%03lld %s %s %s\n", tm.tm_hour, tm.tm_min, tm.tm_sec,
               static_cast<long long>((t / 1'000'000) % 1000), plan_.node.c_str(), rp_.name.c_str(), msg.c_str());
}

void Router::at(std::int64_t epoch_ns, std::function<void()> fn, asio::steady_timer& t) {
  t.expires_after(std::chrono::nanoseconds(std::max<std::int64_t>(0, epoch_ns - now_ns())));
  t.async_wait([self = shared_from_this(), fn = std::move(fn)](const std::error_code& ec) {
    if (!ec) fn();
  });
}

void Router::start() {
  const auto& tm = plan_.timing;
  at(ms_to_ns(tm.establish_at) + establish_offset_ns_, [this] { connect(); }, t_establish_);
  at(ms_to_ns(tm.announce_at), [this] {
    announce_due_ = true;
    if (st_.state == State::Established && !withdraw_due_) begin(false);
  }, t_announce_);
  if (tm.withdraw_at) {
    at(ms_to_ns(*tm.withdraw_at), [this] {
      withdraw_due_ = true;
      if (st_.state == State::Established) begin(true);
    }, t_withdraw_);
  }
  at(ms_to_ns(tm.end_at), [this] { stop_all(); }, t_end_);
}

// ------------------------------------------------------------ connection ---
void Router::connect() {
  if (stopping_) return;
  ++gen_;
  st_.state = State::Connect;
  ++st_.connect_attempts;
  const std::int64_t t0 = now_ns();
  std::error_code ec;
  sock_ = tcp::socket(io_);
  sock_.open(tcp::v4(), ec);
  if (!ec) sock_.bind(tcp::endpoint(asio::ip::address_v4(rp_.ip), 0), ec);
  if (ec) {
    st_.errors.push_back("bind " + bgp::format_ipv4(rp_.ip) + ": " + ec.message());
    log("bind failed: " + ec.message() + " (is the address configured on this node?)");
    down("bind failed");
    return;
  }
  sock_.set_option(tcp::no_delay(true), ec);
  const auto gen = gen_;
  sock_.async_connect(tcp::endpoint(asio::ip::address_v4(plan_.dut_ip), plan_.dut_port),
                      [self = shared_from_this(), gen, t0](const std::error_code& e) {
                        if (gen != self->gen_) return;
                        if (!e && self->st_.t_established == 0) self->st_.t_connect_start = t0;
                        self->on_connected(gen, e);
                      });
}

void Router::on_connected(std::uint64_t, const std::error_code& ec) {
  if (ec) {
    if (st_.connect_attempts <= 3 || st_.connect_attempts % 10 == 0) log("connect: " + ec.message());
    down("connect failed");
    return;
  }
  bgp::Capabilities caps{true, true, true, local_as_};
  send(control(bgp::encode_open(local_as_, plan_.hold_time, rp_.router_id, caps)));
  st_.state = State::OpenSent;
  arm_hold(240);  // RFC 4271: large hold time while waiting for OPEN
  read_loop();
}

void Router::read_loop() {
  const auto gen = gen_;
  sock_.async_read_some(asio::buffer(rxbuf_), [self = shared_from_this(), gen](const std::error_code& ec,
                                                                                std::size_t n) {
    if (gen != self->gen_) return;
    if (ec) {
      if (!self->stopping_) self->down(ec == asio::error::eof ? "peer closed the connection" : "read: " + ec.message());
      return;
    }
    self->rx_time_ = now_ns();
    self->parser_.feed(std::span<const std::uint8_t>(self->rxbuf_.data(), n));
    try {
      while (auto m = self->parser_.next()) {
        self->handle(*m);
        if (gen != self->gen_) return;  // session went down while handling
      }
    } catch (const bgp::DecodeError& e) {
      self->fail(e.code(), e.subcode(), std::string("decode: ") + e.what());
      return;
    }
    self->read_loop();
  });
}

// ------------------------------------------------------------------ FSM ---
void Router::handle(const bgp::Message& m) {
  if (st_.state == State::Established || st_.state == State::OpenConfirm) arm_hold(st_.hold_negotiated);
  switch (m.type) {
    case bgp::MsgType::Open:
      if (st_.state != State::OpenSent) return fail(5, 0, "OPEN in state " + std::string(state_name(st_.state)));
      on_open(bgp::decode_open(m.body));
      break;
    case bgp::MsgType::Keepalive: {
      ++st_.keepalives_rx;
      if (last_keepalive_rx_ && st_.state == State::Established)
        st_.max_keepalive_gap_ns = std::max(st_.max_keepalive_gap_ns, rx_time_ - last_keepalive_rx_);
      last_keepalive_rx_ = rx_time_;
      if (st_.state == State::OpenConfirm) established();
      break;
    }
    case bgp::MsgType::Update:
      if (st_.state != State::Established) return fail(5, 0, "UPDATE before Established");
      on_update(bgp::decode_update(m.body, st_.as4));
      break;
    case bgp::MsgType::Notification: {
      auto n = bgp::decode_notification(m.body);
      st_.notifications_rx.emplace_back(n.code, n.subcode);
      log("NOTIFICATION received: " + bgp::notification_text(n.code, n.subcode));
      down("notification " + std::to_string(n.code) + "/" + std::to_string(n.subcode));
      break;
    }
    case bgp::MsgType::RouteRefresh: break;  // our Adj-RIB-Out is static; nothing to resend
  }
}

void Router::on_open(const bgp::OpenMsg& o) {
  if (o.asn != plan_.dut_asn)
    return fail(2, 2, "peer AS " + std::to_string(o.asn) + " != DUT AS " + std::to_string(plan_.dut_asn));
  const std::uint16_t hold = std::min(plan_.hold_time, o.hold_time);
  if (hold != 0 && hold < 3) return fail(2, 6, "unacceptable hold time");
  st_.peer_asn = o.asn;
  st_.peer_router_id = o.router_id;
  st_.hold_negotiated = hold;
  st_.as4 = o.caps.four_octet_as;
  send(control(bgp::encode_keepalive()));
  st_.state = State::OpenConfirm;
  arm_hold(hold);
}

void Router::established() {
  st_.state = State::Established;
  ++st_.sessions_established;
  if (st_.t_established == 0) st_.t_established = rx_time_;
  log("Established (peer AS " + std::to_string(st_.peer_asn) + ", hold " + std::to_string(st_.hold_negotiated) +
      " s, " + (st_.as4 ? "4-octet AS" : "2-octet AS") + ")");
  arm_keepalive();
  if (withdraw_due_) return;          // after the withdraw phase nothing is announced again
  if (announce_due_) begin(false);    // (re-)announce after a late or repeated establishment
}

// ------------------------------------------------------------- sending ---
void Router::build_batches() {
  if (batches_built_ && batches_as4_ == st_.as4) return;
  ann_.clear();
  wd_.clear();
  auto make = [&](const std::vector<bgp::Bytes>& msgs, std::vector<Batch>& out, bool withdraw) {
    for (const auto& m : msgs) {
      Batch b;
      b.msg = std::make_shared<bgp::Bytes>(m);
      const auto u = bgp::decode_update(std::span<const std::uint8_t>(m).subspan(bgp::kHeaderLen), st_.as4);
      const auto& list = withdraw ? u.withdrawn : u.nlri;
      if (!withdraw) sent_types_ |= u.attr_types;
      b.prefixes = list.size();
      for (const auto& p : list)
        if (sampled(p, plan_.sample_mod)) b.samples.push_back(p);
      out.push_back(std::move(b));
    }
  };
  make(bgp::build_announcements(rp_.prefixes, rp_.attrs, st_.as4, plan_.per_update), ann_, false);
  make(bgp::build_withdrawals(rp_.prefixes, plan_.per_update), wd_, true);
  batches_built_ = true;
  batches_as4_ = st_.as4;
}

void Router::begin(bool withdraw) {
  if (st_.state != State::Established) return;
  build_batches();
  t_pace_.cancel();
  active_ = true;
  withdrawing_ = withdraw;
  next_batch_ = 0;
  pace_start_ = now_ns();
  pace_prefixes_ = 0;
  log(std::string(withdraw ? "withdrawing " : "announcing ") + std::to_string(rp_.prefixes.size()) + " prefixes in " +
      std::to_string((withdraw ? wd_ : ann_).size()) + " UPDATEs" +
      (plan_.announce_rate > 0 ? " at " + std::to_string(static_cast<long>(plan_.announce_rate)) + " prefixes/s"
                               : " at max rate"));
  pump();
}

void Router::pump() {
  if (!active_ || st_.state != State::Established) return;
  auto& batches = withdrawing_ ? wd_ : ann_;
  const Kind kind = withdrawing_ ? Kind::Withdraw : Kind::Announce;
  if (plan_.announce_rate <= 0) {
    // as fast as the session allows: keep a small window queued so the
    // socket is never idle but the queue cannot grow without bound
    while (next_batch_ < batches.size() && outq_.size() < 8) {
      const auto& b = batches[next_batch_++];
      send(Out{b.msg, kind, b.prefixes, b.samples, 0});
    }
  } else {
    const std::int64_t now = now_ns();
    while (next_batch_ < batches.size()) {
      const auto sched = pace_start_ + static_cast<std::int64_t>(pace_prefixes_ / plan_.announce_rate * 1e9);
      if (sched > now) {
        t_pace_.expires_after(std::chrono::nanoseconds(sched - now));
        const auto gen = gen_;
        t_pace_.async_wait([self = shared_from_this(), gen](const std::error_code& ec) {
          if (!ec && gen == self->gen_) self->pump();
        });
        return;
      }
      const auto& b = batches[next_batch_++];
      send(Out{b.msg, kind, b.prefixes, b.samples, sched});
      pace_prefixes_ += static_cast<double>(b.prefixes);
    }
  }
  if (next_batch_ >= batches.size()) active_ = false;
}

void Router::send(Out o) {
  outq_.push_back(std::move(o));
  if (!writing_) write_next();
}

void Router::write_next() {
  if (outq_.empty()) {
    writing_ = false;
    if (active_ && plan_.announce_rate <= 0) pump();
    return;
  }
  writing_ = true;
  const auto gen = gen_;
  asio::async_write(sock_, asio::buffer(*outq_.front().msg),
                    [self = shared_from_this(), gen](const std::error_code& ec, std::size_t n) {
                      if (gen != self->gen_) return;
                      if (ec) {
                        if (!self->stopping_) self->down("write: " + ec.message());
                        return;
                      }
                      const std::int64_t t = now_ns();
                      Out o = std::move(self->outq_.front());
                      self->outq_.pop_front();
                      auto& st = self->st_;
                      st.bytes_sent += n;
                      if (o.kind == Kind::Announce) {
                        if (st.t_first_send == 0) st.t_first_send = t;
                        st.t_last_send = t;
                      } else if (o.kind == Kind::Withdraw) {
                        if (st.t_first_wd_send == 0) st.t_first_wd_send = t;
                        st.t_last_wd_send = t;
                      }
                      if (o.kind != Kind::Control) {
                        auto& samples = o.kind == Kind::Announce ? self->sample_tx_ : self->sample_wd_tx_;
                        for (const auto& p : o.samples) samples.emplace(p.key(), t);
                        if (o.kind == Kind::Announce) { ++st.updates_sent; st.prefixes_sent += o.prefixes; }
                        else { ++st.withdraw_updates_sent; st.prefixes_withdrawn += o.prefixes; }
                        if (o.scheduled) st.send_lag_ns.push_back(t - o.scheduled);
                      }
                      self->write_next();
                    });
}

// ------------------------------------------------------------ receiving ---
void Router::on_update(const bgp::UpdateMsg& u) {
  const std::int64_t t = rx_time_;
  ++st_.updates_rx;
  if (u.withdrawn.empty() && u.nlri.empty()) {  // End-of-RIB marker (RFC 4724)
    ++st_.end_of_rib_rx;
    return;
  }
  if (st_.t_first_rx == 0 && !u.nlri.empty()) st_.t_first_rx = t;
  st_.t_last_rx = t;
  const bool keep = rp_.receive != ReceiveMode::Count;
  for (const auto& p : u.withdrawn) {
    ++st_.withdrawn_rx;
    if (keep) held_.erase(p.key());
    if (sampled(p, plan_.sample_mod)) sample_wd_rx_.emplace(p.key(), t);
  }
  if (!u.nlri.empty()) {
    const auto n = u.nlri.size();
    const auto& path = u.attrs.as_path;
    // Loop detection like a real router (RFC 4271 9.1.2, RFC 4456 8): a route
    // carrying our own AS (eBGP) or our own ORIGINATOR_ID (iBGP via a reflector)
    // is unusable - treated as withdrawn and counted, not held. Some DUTs (e.g.
    // FRR) send routes back to their originator and rely on this check.
    const bool looped = plan_.role == DutRole::RouteReflector
                            ? (u.attrs.originator_id && *u.attrs.originator_id == rp_.router_id)
                            : std::find(path.begin(), path.end(), rp_.asn) != path.end();
    if (looped) {
      st_.looped_rx += n;
      if (keep)
        for (const auto& p : u.nlri) held_.erase(p.key());
      after_rx(t, keep);
      return;
    }
    if (!path.empty() && path.front() == plan_.dut_asn) st_.paths_with_dut_asn += n;
    for (int ty = 0; ty < 256; ++ty)
      if (u.attr_types[static_cast<std::size_t>(ty)]) st_.attr_rx[ty] += n;
    if (u.attrs.has_next_hop && u.attrs.next_hop == plan_.dut_ip) st_.next_hop_is_dut += n;
    if (plan_.role == DutRole::RouteReflector && !u.attrs.originator_id) st_.paths_without_originator += n;
    for (const auto& p : u.nlri) {
      ++st_.nlri_rx;
      if (keep) held_.insert(p.key());
      if (sampled(p, plan_.sample_mod)) sample_rx_.emplace(p.key(), t);  // first arrival only
    }
  }
  after_rx(t, keep);
}

void Router::after_rx(std::int64_t t, bool keep) {
  st_.routes_held = keep ? held_.size()
                         : (st_.nlri_rx > st_.withdrawn_rx ? st_.nlri_rx - st_.withdrawn_rx : 0);
  st_.routes_peak = std::max(st_.routes_peak, st_.routes_held);
  if (rp_.expected_rx && st_.t_converged == 0 && st_.routes_held >= rp_.expected_rx) {
    st_.t_converged = t;
    log("converged: " + std::to_string(st_.routes_held) + " routes");
  }
  if (withdraw_due_ && st_.routes_held == 0 && st_.t_empty_after_withdraw == 0 && st_.routes_peak > 0)
    st_.t_empty_after_withdraw = t;
}

// --------------------------------------------------------------- timers ---
void Router::arm_hold(int seconds) {
  t_hold_.cancel();
  if (seconds <= 0) return;
  t_hold_.expires_after(std::chrono::seconds(seconds));
  const auto gen = gen_;
  t_hold_.async_wait([self = shared_from_this(), gen](const std::error_code& ec) {
    if (!ec && gen == self->gen_) self->fail(4, 0, "hold timer expired");
  });
}

void Router::arm_keepalive() {
  t_keepalive_.cancel();
  if (st_.hold_negotiated == 0) return;
  t_keepalive_.expires_after(std::chrono::milliseconds(st_.hold_negotiated * 1000 / 3));
  const auto gen = gen_;
  t_keepalive_.async_wait([self = shared_from_this(), gen](const std::error_code& ec) {
    if (ec || gen != self->gen_ || self->st_.state != State::Established) return;
    self->send(control(bgp::encode_keepalive()));
    self->arm_keepalive();
  });
}

// ------------------------------------------------------------ teardown ---
void Router::fail(std::uint8_t code, std::uint8_t subcode, const std::string& why) {
  st_.notifications_tx.emplace_back(code, subcode);
  st_.errors.push_back(why);
  log("sending NOTIFICATION " + bgp::notification_text(code, subcode) + ": " + why);
  std::error_code ec;
  auto n = bgp::encode_notification(code, subcode);
  asio::write(sock_, asio::buffer(n), ec);
  down(why);
}

void Router::down(const std::string& why) {
  if (st_.state == State::Idle || st_.state == State::Stopped) return;
  if (st_.state == State::Established) {
    ++st_.resets;
    log("session down: " + why);
  }
  ++gen_;  // invalidates every pending callback of this connection
  std::error_code ec;
  sock_.close(ec);
  for (auto* t : {&t_hold_, &t_keepalive_, &t_pace_}) t->cancel();
  outq_.clear();
  writing_ = false;
  active_ = false;
  parser_ = bgp::StreamParser();
  held_.clear();
  st_.routes_held = 0;
  last_keepalive_rx_ = 0;
  st_.state = State::Idle;
  if (stopping_) return;
  t_retry_.expires_after(std::chrono::seconds(plan_.connect_retry_s));
  t_retry_.async_wait([self = shared_from_this()](const std::error_code& e) {
    if (!e) self->connect();
  });
}

void Router::stop_all() {
  stopping_ = true;
  if (st_.state == State::Established) {
    std::error_code ec;
    auto n = bgp::encode_notification(6, 2);  // Cease: administrative shutdown
    asio::write(sock_, asio::buffer(n), ec);
    st_.notifications_tx.emplace_back(6, 2);
  }
  ++gen_;
  std::error_code ec;
  sock_.close(ec);
  for (auto* t : {&t_establish_, &t_announce_, &t_withdraw_, &t_end_, &t_hold_, &t_keepalive_, &t_retry_, &t_pace_})
    t->cancel();
  outq_.clear();
  if (st_.state != State::Idle) log(std::string("stopped in state ") + state_name(st_.state));
  st_.state = State::Stopped;
}

// -------------------------------------------------------------- result ---
json Router::result() const {
  auto pct = [](std::vector<std::int64_t> v, double q) -> double {
    if (v.empty()) return 0;
    std::sort(v.begin(), v.end());
    return static_cast<double>(v[std::min(v.size() - 1, static_cast<std::size_t>(q * (v.size() - 1) + 0.5))]) / 1e6;
  };
  auto samples = [](const std::unordered_map<std::uint64_t, std::int64_t>& m) {
    json a = json::array();
    for (const auto& [k, t] : m) a.push_back({k, t});
    return a;
  };
  json notif_rx = json::array(), notif_tx = json::array();
  for (auto [c, s] : st_.notifications_rx) notif_rx.push_back({c, s});
  for (auto [c, s] : st_.notifications_tx) notif_tx.push_back({c, s});
  return {
      {"name", rp_.name}, {"asn", rp_.asn}, {"ip", bgp::format_ipv4(rp_.ip)},
      {"prefixes_configured", rp_.prefixes.size()}, {"expected_rx", rp_.expected_rx},
      {"state", state_name(st_.state)}, {"connect_attempts", st_.connect_attempts},
      {"sessions_established", st_.sessions_established}, {"resets", st_.resets},
      {"t_connect_start", st_.t_connect_start}, {"t_established", st_.t_established},
      {"peer_asn", st_.peer_asn}, {"peer_router_id", bgp::format_ipv4(st_.peer_router_id)},
      {"hold", st_.hold_negotiated}, {"as4", st_.as4},
      {"updates_sent", st_.updates_sent}, {"prefixes_sent", st_.prefixes_sent},
      {"withdraw_updates_sent", st_.withdraw_updates_sent}, {"prefixes_withdrawn", st_.prefixes_withdrawn},
      {"bytes_sent", st_.bytes_sent}, {"t_first_send", st_.t_first_send}, {"t_last_send", st_.t_last_send},
      {"t_first_wd_send", st_.t_first_wd_send}, {"t_last_wd_send", st_.t_last_wd_send},
      {"send_lag_ms", {{"n", st_.send_lag_ns.size()}, {"p50", pct(st_.send_lag_ns, 0.5)},
                       {"p99", pct(st_.send_lag_ns, 0.99)}, {"max", pct(st_.send_lag_ns, 1.0)}}},
      {"updates_rx", st_.updates_rx}, {"nlri_rx", st_.nlri_rx}, {"withdrawn_rx", st_.withdrawn_rx},
      {"keepalives_rx", st_.keepalives_rx}, {"end_of_rib_rx", st_.end_of_rib_rx}, {"routes_held", st_.routes_held}, {"routes_peak", st_.routes_peak},
      {"t_first_rx", st_.t_first_rx}, {"t_converged", st_.t_converged}, {"t_last_rx", st_.t_last_rx},
      {"t_empty_after_withdraw", st_.t_empty_after_withdraw},
      {"checks", {{"paths_with_dut_asn", st_.paths_with_dut_asn}, {"next_hop_is_dut", st_.next_hop_is_dut},
                  {"paths_without_originator", st_.paths_without_originator}, {"looped_rx", st_.looped_rx}}},
      {"attrs_sent", [&] { json a = json::array(); for (int ty = 0; ty < 256; ++ty) if (sent_types_[static_cast<std::size_t>(ty)]) a.push_back(ty); return a; }()},
      {"attrs_rx", [&] { json o = json::object(); for (const auto& [ty, n] : st_.attr_rx) o[std::to_string(ty)] = n; return o; }()},
      {"max_keepalive_gap_ms", st_.max_keepalive_gap_ns / 1e6},
      {"notifications_rx", notif_rx}, {"notifications_tx", notif_tx}, {"errors", st_.errors},
      {"samples", {{"tx", samples(sample_tx_)}, {"rx", samples(sample_rx_)},
                   {"wd_tx", samples(sample_wd_tx_)}, {"wd_rx", samples(sample_wd_rx_)}}},
  };
}

}  // namespace tgen
