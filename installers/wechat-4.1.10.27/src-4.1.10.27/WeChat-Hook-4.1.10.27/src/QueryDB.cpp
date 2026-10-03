#include "httplib.h"
#include "json.hpp"
#include <windows.h>
#include "tools.h"
#include "global.h"
#include "db_mgr.h"

#include "QueryDB.h"


using json = nlohmann::json;
//static xmgr::DatabaseMgr& g_dbMgr = xmgr::DatabaseMgr::getInstance();

static std::string BytesToHex(const uint8_t* data, size_t len)
{
    static const char hex[] = "0123456789ABCDEF";
    std::string out;
    out.reserve(len * 2);

    for (size_t i = 0; i < len; ++i)
    {
        out.push_back(hex[data[i] >> 4]);
        out.push_back(hex[data[i] & 0x0F]);
    }
    return out;
}


void Route_QueryDB(httplib::Server& svr)
{
    svr.Post("/QueryDB/execute", [](const httplib::Request& req, httplib::Response& res)
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


            //初始化数据库管理器
            //OutputDebugStringA("[QueryDB] 初始化数据库管理器\n");
            xmgr::DatabaseMgr& g_dbMgr = xmgr::DatabaseMgr::getInstance();
            //OutputDebugStringA("[QueryDB] 初始化完成\n");


            std::string optDbName = reqJson.value("optDbName", "");         // MicroMsg.db
            std::string sql = reqJson.value("SQL", "");                     // 查询语句   SELECT * FROM ChatRoom LIMIT 10

            auto result = g_dbMgr.execute(optDbName, sql);


            // 使用临界区保护数据库访问
            /*
            EnterCriticalSection(&g_dbMgrCriticalSection);
            try {
                auto start = std::chrono::high_resolution_clock::now();
                auto result = g_dbMgr.execute(optDbName, sql);
                auto end = std::chrono::high_resolution_clock::now();

                auto duration = std::chrono::duration_cast<std::chrono::milliseconds>(end - start);

                resp["ret"] = result.value("status", 0);
                resp["msg"] = result.value("desc", "");
                resp["data"] = result.value("data", json::array());
                resp["duration_ms"] = duration.count();

                // 慢查询日志
                if (duration.count() > 1000) {
                    OutputDebugStringA(format_string("Slow query: %lldms - %s", duration.count(), sql.substr(0, 100)).c_str());
                }

            }
            catch (const std::exception& e) {
                resp["ret"] = -500;
                resp["msg"] = std::string("Database error: ") + e.what();
            }
            LeaveCriticalSection(&g_dbMgrCriticalSection);
            */


            res.set_content(result.dump(4, ' ', false), "application/json");


            //res.set_content(resp.dump(), "application/json");
        });
    svr.Post("/QueryDB/GetAllDBName", [](const httplib::Request& req, httplib::Response& res)
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


            //初始化数据库管理器
            //OutputDebugStringA("[QueryDB] 初始化数据库管理器\n");
            xmgr::DatabaseMgr& g_dbMgr = xmgr::DatabaseMgr::getInstance();
            //OutputDebugStringA("[QueryDB] 初始化完成\n");

            // 方式2：获取所有数据库信息
            auto dbInfo = g_dbMgr.getDatabaseInfo();
            // 返回：{"dbName":"MicroMsg.db","dbHandle":140736870540544}

            res.set_content(dbInfo.dump(4, ' ', false), "application/json");


            //res.set_content(resp.dump(), "application/json");
        });

    // 添加状态查询接口
    svr.Get("/QueryDB/status", [](const httplib::Request&, httplib::Response& res) {
        json resp;
        resp["IsLogin"] = g_IsLogin;
        resp["hWeixin"] = (uint64_t)g_hWeixinDll;
        // 「登录就绪」判据的**可读诊断**（空串 = 正常）。
        // 为什么要有它：数据被搬到别的盘之后，判据会**永远不成立**，IsLogin 恒为 0、
        // 所有查询回 "get database handle ... failed"，而表面上不报任何错。
        // 把原因摆在这儿，用户/上层一眼就能看到，不用猜是不是「掉登录」了。
        resp["LoginGate"] = LoginGateNote();
        res.set_content(resp.dump(4, ' ', false), "application/json");
        });


}
