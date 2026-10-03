#pragma once
#include <string>

namespace WeixinSend
{
    void SendImage(const std::string& wxid, const std::string& imgPath);
    void SendText(const std::string& wxidorgid, const std::string& msg);
    void DecodePic(const std::string& enc_pic_path, const std::string& dec_pic_path);

    // ---- 语音通话邀请（2026-10-03 逆向出来的，见 _audit/通话功能-逆向进度与恢复.md）----
    // `type` 是消息类型：1 = 文本、6 = 图片、**50(0x32) = 语音邀请**。
    // 为什么把 type/body 做成参数而不是写死：真机上"类型 50 这样发出去能不能把
    // 对方叫响"**必须实测**，而每改一次常量都要重编 DLL + 重启微信（每次重启都要
    // 重新扫码）。做成参数就一个 HTTP 调用试一个取值，不用重启。
    void SendTyped(const std::string& wxid, const std::string& body, uint64_t type);
    void SendVoipInvite(const std::string& peerWxid);

    // 用**微信自己的原语**（0x2D8 通用消息对象 + 0xA04560 构造 + 0xA1B1B0 分发）
    // 造一条消息发出去。和 SendTyped 的区别是**对象类**：SendTyped 用 TextMessage，
    // 本函数用真机通话时抓到的那种对象（虚表 base+0x81D2458，大小 0x2D8）。
    // `selfWxid` 不给就只设对方 wxid（微信可能自己补发送方）。
    void SendVoipObject(const std::string& selfWxid, const std::string& peerWxid,
                        const std::string& body, uint64_t type);
    void SendVoipObjectInvite(const std::string& selfWxid, const std::string& peerWxid);
}
