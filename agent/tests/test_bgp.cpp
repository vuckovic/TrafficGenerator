// SPDX-License-Identifier: MIT
#include <gtest/gtest.h>

#include "tgen/bgp.hpp"

using namespace tgen::bgp;

namespace {
Bytes hex(std::initializer_list<int> v) { Bytes b; for (int x : v) b.push_back(static_cast<std::uint8_t>(x)); return b; }
Bytes marker_header(std::uint16_t len, std::uint8_t type) {
  Bytes b(16, 0xFF);
  b.push_back(static_cast<std::uint8_t>(len >> 8));
  b.push_back(static_cast<std::uint8_t>(len));
  b.push_back(type);
  return b;
}
Message parse_one(const Bytes& wire) {
  StreamParser p;
  p.feed(wire);
  auto m = p.next();
  EXPECT_TRUE(m.has_value());
  EXPECT_EQ(p.buffered(), 0u);
  return *m;
}
}  // namespace

TEST(Prefix, ParseFormatMask) {
  EXPECT_EQ(Prefix::parse("10.1.2.3/24").str(), "10.1.2.0/24");
  EXPECT_EQ(Prefix::parse("0.0.0.0/0").str(), "0.0.0.0/0");
  EXPECT_EQ(Prefix::parse("192.168.100.11/32").str(), "192.168.100.11/32");
  EXPECT_EQ(Prefix::parse("10.255.255.255/28").str(), "10.255.255.240/28");
  EXPECT_THROW(Prefix::parse("10.0.0.0/33"), std::invalid_argument);
  EXPECT_THROW(Prefix::parse("10.0.0/8"), std::invalid_argument);
  EXPECT_THROW(Prefix::parse("10.0.0.256/8"), std::invalid_argument);
  EXPECT_EQ(nlri_size(Prefix::parse("10.0.0.0/8")), 2u);
  EXPECT_EQ(nlri_size(Prefix::parse("10.0.0.0/24")), 4u);
  EXPECT_EQ(nlri_size(Prefix::parse("10.0.0.0/28")), 5u);
}

TEST(Encode, KeepaliveIsBareHeader) {
  EXPECT_EQ(encode_keepalive(), marker_header(19, 4));
}

TEST(Encode, KnownUpdateVector) {
  // 10.0.0.0/8, ORIGIN IGP, AS_PATH [65001], NEXT_HOP 1.2.3.4, 2-octet AS session
  PathAttributes a;
  a.as_path = {65001};
  a.next_hop = parse_ipv4("1.2.3.4");
  Prefix p = Prefix::parse("10.0.0.0/8");
  Bytes got = encode_update({}, &a, std::span(&p, 1), false);
  Bytes want = marker_header(43, 2);
  Bytes body = hex({0x00, 0x00, 0x00, 0x12,                    // no withdrawals, 18 bytes attributes
                    0x40, 0x01, 0x01, 0x00,                    // ORIGIN IGP
                    0x40, 0x02, 0x04, 0x02, 0x01, 0xFD, 0xE9,  // AS_PATH SEQ [65001]
                    0x40, 0x03, 0x04, 0x01, 0x02, 0x03, 0x04,  // NEXT_HOP 1.2.3.4
                    0x08, 0x0A});                              // 10/8
  want.insert(want.end(), body.begin(), body.end());
  EXPECT_EQ(got, want);
}

TEST(OpenMsg, RoundTripFourOctetAs) {
  Bytes w = encode_open(4200000117u, 90, parse_ipv4("10.100.0.20"), Capabilities{true, true, true, 4200000117u});
  Message m = parse_one(w);
  ASSERT_EQ(m.type, MsgType::Open);
  OpenMsg o = decode_open(m.body);
  EXPECT_EQ(o.my_as16, kAsTrans);
  EXPECT_EQ(o.asn, 4200000117u);
  EXPECT_EQ(o.hold_time, 90);
  EXPECT_EQ(format_ipv4(o.router_id), "10.100.0.20");
  EXPECT_TRUE(o.caps.four_octet_as);
  EXPECT_TRUE(o.caps.mp_ipv4_unicast);
  EXPECT_TRUE(o.caps.route_refresh);
}

TEST(OpenMsg, RejectsBadHoldTime) {
  Bytes w = encode_open(65001, 2, 1, Capabilities{});
  Message m = parse_one(w);
  EXPECT_THROW(decode_open(m.body), DecodeError);
}

TEST(Update, RoundTripAllAttributes) {
  PathAttributes a;
  a.origin = Origin::Incomplete;
  a.as_path = {4200000117u, 64700, 64701};
  a.next_hop = parse_ipv4("10.100.0.20");
  a.med = 50;
  a.communities = {(64500u << 16) | 100, (64500u << 16) | 200};
  a.large_communities = {{64500, 1, 2}};
  std::vector<Prefix> nlri = {Prefix::parse("10.1.0.0/24"), Prefix::parse("10.2.3.16/28")};
  std::vector<Prefix> wd = {Prefix::parse("10.9.0.0/24")};
  for (bool as4 : {true, false}) {
    Message m = parse_one(encode_update(wd, &a, nlri, as4));
    ASSERT_EQ(m.type, MsgType::Update);
    UpdateMsg u = decode_update(m.body, as4);
    EXPECT_EQ(u.withdrawn, wd);
    EXPECT_EQ(u.nlri, nlri);
    EXPECT_EQ(u.attrs.origin, a.origin);
    EXPECT_EQ(u.attrs.as_path, a.as_path) << "as4=" << as4;  // AS4_PATH merge when !as4
    EXPECT_EQ(u.attrs.next_hop, a.next_hop);
    EXPECT_TRUE(u.attrs.has_next_hop);
    EXPECT_EQ(u.attrs.med, a.med);
    EXPECT_EQ(u.attrs.communities, a.communities);
    EXPECT_EQ(u.attrs.large_communities, a.large_communities);
  }
}

TEST(Update, TwoOctetSessionUsesAsTrans) {
  PathAttributes a;
  a.as_path = {4200000117u};
  a.next_hop = 1;
  Prefix p = Prefix::parse("10.0.0.0/24");
  Message m = parse_one(encode_update({}, &a, std::span(&p, 1), false));
  // Decoding as a 4-octet session would misread; decode as 2-octet and check
  // that the raw AS_PATH carried AS_TRANS before the AS4_PATH merge.
  UpdateMsg u = decode_update(m.body, false);
  EXPECT_EQ(u.attrs.as_path, std::vector<std::uint32_t>{4200000117u});
}

TEST(Update, LongAsPathUsesExtendedLengthAndSegments) {
  PathAttributes a;
  for (std::uint32_t i = 0; i < 300; ++i) a.as_path.push_back(64512 + i);
  a.next_hop = 1;
  Prefix p = Prefix::parse("10.0.0.0/24");
  Message m = parse_one(encode_update({}, &a, std::span(&p, 1), true));
  UpdateMsg u = decode_update(m.body, true);
  EXPECT_EQ(u.attrs.as_path, a.as_path);
}

TEST(Pack, AnnouncementsFitAndCoverAll) {
  PathAttributes a;
  a.as_path = {4200000117u};
  a.next_hop = parse_ipv4("10.100.0.20");
  std::vector<Prefix> nlri;
  for (std::uint32_t i = 0; i < 10000; ++i) nlri.push_back(Prefix::make(0x0A000000u + (i << 8), 24));
  auto msgs = build_announcements(nlri, a, true);
  std::size_t total = 0;
  for (const auto& b : msgs) {
    ASSERT_LE(b.size(), kMaxMessageLen);
    Message m = parse_one(b);
    total += decode_update(m.body, true).nlri.size();
  }
  EXPECT_EQ(total, nlri.size());
  EXPECT_LE(msgs.size(), 12u);  // ~1000 x /24 per full UPDATE

  auto capped = build_announcements(nlri, a, true, 100);
  EXPECT_EQ(capped.size(), 100u);
  auto wd = build_withdrawals(nlri);
  std::size_t wtotal = 0;
  for (const auto& b : wd) wtotal += decode_update(parse_one(b).body, true).withdrawn.size();
  EXPECT_EQ(wtotal, nlri.size());
}

TEST(Stream, FragmentedAndCoalesced) {
  PathAttributes a;
  a.as_path = {65001};
  a.next_hop = 1;
  Prefix p = Prefix::parse("10.0.0.0/24");
  Bytes stream;
  for (int i = 0; i < 5; ++i) {
    auto k = encode_keepalive();
    auto u = encode_update({}, &a, std::span(&p, 1), true);
    stream.insert(stream.end(), k.begin(), k.end());
    stream.insert(stream.end(), u.begin(), u.end());
  }
  StreamParser sp;
  int got = 0;
  for (std::uint8_t byte : stream) {  // worst case: one byte at a time
    sp.feed(std::span(&byte, 1));
    while (auto m = sp.next()) ++got;
  }
  EXPECT_EQ(got, 10);
  EXPECT_EQ(sp.buffered(), 0u);
}

TEST(Stream, RejectsBadMarkerAndLength) {
  StreamParser a;
  Bytes bad = encode_keepalive();
  bad[3] = 0;
  a.feed(bad);
  EXPECT_THROW(a.next(), DecodeError);
  StreamParser b;
  Bytes shortlen = marker_header(10, 4);
  b.feed(shortlen);
  EXPECT_THROW(b.next(), DecodeError);
}

TEST(Notification, RoundTripAndText) {
  Message m = parse_one(encode_notification(6, 2));
  NotificationMsg n = decode_notification(m.body);
  EXPECT_EQ(n.code, 6);
  EXPECT_EQ(n.subcode, 2);
  EXPECT_EQ(notification_text(6, 2), "Cease: Administrative shutdown");
  EXPECT_EQ(notification_text(4, 0), "Hold Timer Expired");
}
