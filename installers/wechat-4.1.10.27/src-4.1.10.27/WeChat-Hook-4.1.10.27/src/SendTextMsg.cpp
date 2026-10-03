#include "httplib.h"
#include "json.hpp"
#include <windows.h>
#include "wx_send.h"
#include "SendTextMsg.h"

using json = nlohmann::json;

void Route_SendTextMsg(httplib::Server& svr)
{
    svr.Post("/SendTextMsg", [](const httplib::Request& req, httplib::Response& res)
        {
            json reqJson;
            json resp;

            try
            {
                reqJson = json::parse(req.body);
            }
            catch (...)
            {
                resp["ret"] = -1;
                resp["msg"] = "invalid json";
                res.set_content(resp.dump(), "application/json");
                return;
            }

            std::string wxidorgid = reqJson.value("wxidorgid", "");
            std::string msg = reqJson.value("msg", "");

            WeixinSend::SendText(wxidorgid, msg);


            resp["ret"] = 0;
            resp["retmsg"] = "success";

            res.set_content(resp.dump(), "application/json");
        });

    svr.Post("/Decode_Pic", [](const httplib::Request& req, httplib::Response& res)
        {
            json reqJson;
            json resp;

            try
            {
                reqJson = json::parse(req.body);
            }
            catch (...)
            {
                resp["ret"] = -1;
                resp["msg"] = "invalid json";
                res.set_content(resp.dump(), "application/json");
                return;
            }
			

            std::string src_path = reqJson.value("src_path", "");
            std::string dst_path = reqJson.value("dst_path", "");

            OutputDebugStringA(("Decode_Pic src_path: " + src_path + "\n").c_str());
            OutputDebugStringA(("Decode_Pic dst_path: " + dst_path + "\n").c_str());


            WeixinSend::DecodePic(src_path, dst_path);


            resp["ret"] = 0;
            resp["retmsg"] = "success";

            res.set_content(resp.dump(), "application/json");
        });

    // ============================================================
    // /CallVoip —— 发起语音通话（2026-10-03 逆向出来的）
    // ============================================================
    // 原理与证据见 _audit/通话功能-逆向进度与恢复.md 第二十三轮：
    // 微信发起语音通话 = 发一条 **类型 50(0x32)** 的消息，正文是那段 277 字节的
    // 邀请 XML（全是常量或 0，没有服务端一次性数据）。
    //
    // 参数：
    //   wxid  必填，对方 wxid
    //   type  可选，默认 50。**故意可调**：真机要试不同取值，而每改一次常量都得
    //         重编 DLL + 重启微信（每次重启都要重新扫码）；做成参数就一个 HTTP
    //         调用试一个，不用重启。
    //   body  可选。不给就用内置邀请 XML；给了原样当正文 —— 同样是为了试不同拼法。
    //
    // ⚠️ 这个端点**会真的发一条消息出去，不可逆**。它自己不弹确认，和 /SendTextMsg
    //    一样 —— 确认闸门在 Python 侧（callgate.py 的三道闸：开关/静默时段/每日上限），
    //    跟「发消息必须用户确认」那条铁律保持一致。别把闸门加到这一层，
    //    否则人手调这个端点会被拦，而机器那侧反而以为"发过了"。
    svr.Post("/CallVoip", [](const httplib::Request& req, httplib::Response& res)
        {
            json reqJson;
            json resp;

            try
            {
                reqJson = json::parse(req.body);
            }
            catch (...)
            {
                resp["ret"] = -1;
                resp["msg"] = "invalid json";
                res.set_content(resp.dump(), "application/json");
                return;
            }

            std::string wxid = reqJson.value("wxid", "");
            if (wxid.empty())
            {
                resp["ret"] = -1;
                resp["msg"] = "wxid required";
                res.set_content(resp.dump(), "application/json");
                return;
            }

            int typeI = reqJson.value("type", 50);
            std::string body = reqJson.value("body", "");
            std::string selfWxid = reqJson.value("self", "");
            // `via` 选走哪条实现：
            //   "text"   （默认）复用文本消息那套 send_message，对象是 TextMessage
            //   "object" 用**微信自己的原语**：0x2D8 通用消息对象 + 0xA04560 + 0xA1B1B0
            // 实测（2026-10-03）"text" 那条 type=50 会被当类型 50 处理、但不进入通话状态；
            // "object" 用的才是真机通话里那种对象。做成开关是因为**两条都要能试**，
            // 而每改一次都要重编 DLL + 重启微信 + 重新扫码。
            std::string via = reqJson.value("via", "text");

            OutputDebugStringA(("[CallVoip] wxid=" + wxid + " type=" +
                std::to_string(typeI) + " via=" + via + " bodyLen=" +
                std::to_string(body.size()) + "\n").c_str());

            if (via == "object")
            {
                if (body.empty())
                {
                    WeixinSend::SendVoipObjectInvite(selfWxid, wxid);
                }
                else
                {
                    WeixinSend::SendVoipObject(selfWxid, wxid, body,
                                               (uint64_t)(unsigned int)typeI);
                }
            }
            else if (body.empty())
            {
                WeixinSend::SendVoipInvite(wxid);
            }
            else
            {
                WeixinSend::SendTyped(wxid, body, (uint64_t)(unsigned int)typeI);
            }

            resp["ret"] = 0;
            resp["retmsg"] = "success";

            res.set_content(resp.dump(), "application/json");
        });

}
