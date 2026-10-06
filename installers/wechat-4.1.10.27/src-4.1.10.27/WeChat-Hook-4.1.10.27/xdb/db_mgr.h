#pragma once
#include "sqlite3.h"
#include <json/json.hpp>
#include <iostream>
#include <map>
#include <mutex>

namespace xmgr {
    class DatabaseMgr
    {
    public:
        static DatabaseMgr& getInstance() {
            static DatabaseMgr cls;
            return cls;
        }
        const std::map<std::string, LPVOID>& searchDatabases();
        LPVOID getDatabaseHandle(const std::string& dbname);
        // 句柄表存活校验（给就绪判据/看门狗用，2026-10-05 加）：
        // 只读自己的缓存 + `__try` 探每个句柄；**不 clear、不扫描**。
        bool HandleTableAlive();
        // 只读快照：库名 -> 句柄数值（同样不重建缓存）
        void HandlesSnapshot(std::vector<std::pair<std::string, uint64_t>>& out);
        // 强制重扫：清缓存 + 真扫一遍（重活，只给就绪判据的冷路径用）
        void ForceRescan();
        std::string codec_get_key(const std::string& dbname);
        std::string codec_get_key(sqlite3* db);
        nlohmann::ordered_json execute(const std::string& dbname, const std::string& sql);
        nlohmann::ordered_json execute(sqlite3* db, const std::string& sql);
        int backup(const std::string& dbname, const std::string& out_path);
        int backup(sqlite3* db, const std::string& out_path);
        void backup_xprogress(int remaining, int pagecount);
        sqlite3_api_routines* getSqlite3Rountines() const {
            return m_sqlite3Rountines;
        }
        sqlcipher_api_routines* getSqlcipherRountines() const {
            return m_sqlcipherRountines;
        }
        nlohmann::ordered_json getDatabaseInfo();
    private:
        DatabaseMgr();
        ~DatabaseMgr();
        DatabaseMgr& operator=(const DatabaseMgr&) = delete;
        DatabaseMgr(const DatabaseMgr&) = delete;
        sqlite3_api_routines* m_sqlite3Rountines = nullptr;
        sqlcipher_api_routines* m_sqlcipherRountines = nullptr;
        sqlite3CodecGetKey m_codecGetKey = nullptr;
        std::map<std::string, LPVOID> m_dbs;
        std::mutex m_searchDbMtx;
        std::vector<BYTE> m_backupAsmCode;
    };
}