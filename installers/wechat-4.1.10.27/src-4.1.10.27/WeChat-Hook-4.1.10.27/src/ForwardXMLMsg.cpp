#include "json.hpp"
#include "httplib.h"

#include <windows.h>
#include "wx_send_xml.h"

#include "ForwardXMLMsg.h"



using json = nlohmann::json;

void Route_ForwardXMLMsg(httplib::Server& svr)
{
    svr.Post("/ForwardXMLMsg", [](const httplib::Request& req, httplib::Response& res)
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

            std::string wxid = reqJson.value("to_wxid", "");
            std::string xml = reqJson.value("content", "");


            // 后续调用
            WeixinSendXML::Initialize();

            // ⚠️ 转发调用必须也在 try 里。以前只有 JSON 解析被包住，
            // ForwardXmlMessage 一抛异常（真机踩过：图片 XML 缺 hdlength 时
            // ExtractXmlFields 里 std::stoi("") 抛 std::invalid_argument）就冒到
            // httplib，调用方只看到一个没头没脑的 **HTTP 500**，连 ret 都拿不到，
            // 分不清「参数不对」「这条消息不支持」还是「微信里没发出去」。
            // 现在失败一律回 JSON（ret=1），让上层能如实说话。
            bool success = false;
            try
            {
                success = WeixinSendXML::ForwardXmlMessage(wxid, xml);
            }
            catch (...)
            {
                success = false;
            }

            if (success) {
                resp["ret"] = 0;
                resp["retmsg"] = "success";
            }
            else {
                resp["ret"] = 1;
                resp["retmsg"] = "fail";
            }

            
            

            res.set_content(resp.dump(), "application/json");
        });
}
 