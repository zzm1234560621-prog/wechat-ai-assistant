#include "db_mgr.h"
#include "xwechat_offsets.h"
#include "SundaySearch.h"
#include "global.h"
#include "tools.h"

// db 层门禁的唯一 owner（声明在 global.h）。
// 为什么放在这里而不是 global.cpp：门禁的语义归属是「数据库层」，db_mgr 是它的消费者，
// 放在同一个编译单元里读侧就近、也不会让 global.cpp 依赖 db_mgr 的私有状态。
bool DbLayerReady()
{
	return g_DbLayerReady != 0;
}

static void codec_get_key(sqlite3CodecGetKey func, sqlite3* db, int index, void** pKey, int* pLen) {
	__try {
		func(db, index, pKey, pLen);
	}
	__except (EXCEPTION_EXECUTE_HANDLER) {
		//这里输出异常调试信息 
	}
}

namespace xmgr {

	// db 层门禁的唯一读点（2026-10-05）：**不再直接读 g_IsLogin**。
	// 为什么要有它：g_IsLogin 以前只置 1、从不置回 0，于是掉登录 / 句柄表被重建之后，
	// 门禁还是开着的，查询一路拿着空/坏句柄往下走，而对外还报 IsLogin:1 —— 现场最难查的形态。
	// 现在门禁由 g_DbLayerReady 代表，它只有在「文件侧连续新鲜 + 句柄真能查」之后才置 1，
	// 句柄表失效时由看门狗置回 0。
	static bool DbReady()
	{
		return g_DbLayerReady != 0;
	}

	// 句柄表扫描许可（**不是**查询门禁）：见 global.h 里 g_HandleScanAllowed 的说明。
	// 就绪判据必须能扫，否则它永远判不出「句柄能不能用」——那是循环依赖（真踩过）。
	static bool HandleScanAllowed()
	{
		return g_HandleScanAllowed != 0;
	}

	// 句柄表存活校验：只读缓存、只做「地址能不能读」这一件事。
	// 不用 IsBadReadPtr（本工程的 sqlite3.h 里并没有这个符号的定义，它是历史遗留调用），
	// 改用 __try/__except —— 同文件 codec_get_key 已经是这个写法。
	static bool HandleLooksAlive(LPVOID h)
	{
		if (h == nullptr)
			return false;
		__try {
			volatile unsigned char probe = *(volatile unsigned char*)h;
			(void)probe;
			return true;
		}
		__except (EXCEPTION_EXECUTE_HANDLER) {
			return false;
		}
	}

	static std::string toHexString(const std::string& str)
	{
		static const char hex[] = "0123456789ABCDEF";
		std::string out;
		out.reserve(str.size() * 2);

		for (size_t i = 0; i < str.size(); ++i)
		{
			out.push_back(hex[(unsigned char)str[i] >> 4]);
			out.push_back(hex[(unsigned char)str[i] & 0x0F]);
		}
		return out;
	}

	DatabaseMgr::DatabaseMgr() {
		size_t module_base = (size_t)g_hWeixinDll;
		m_sqlite3Rountines = (sqlite3_api_routines*)(module_base + XWECHAT_SQLITE3_API_ROUTINES_OFFSET);
		m_sqlcipherRountines = (sqlcipher_api_routines*)(module_base + XWECHAT_SQLCIPHER_API_ROUTINES_OFFSET);
		m_codecGetKey = (sqlite3CodecGetKey)(module_base + XWECHAT_SQLITE3_CODEC_GET_KEY_FUNC);
		LPVOID patchAddress = (LPVOID)((size_t)m_sqlite3Rountines->backup_init + 0xAD);
		std::vector<BYTE> nopData(14, 0x90);
		m_backupAsmCode.resize(nopData.size(), 0);
		ReadProcessMemory(GetCurrentProcess(), patchAddress, m_backupAsmCode.data(), m_backupAsmCode.size(), nullptr);
		WriteProcessMemory(GetCurrentProcess(), patchAddress, (LPCVOID)nopData.data(), nopData.size(), 0);
	}

	DatabaseMgr::~DatabaseMgr() {
		if (m_sqlite3Rountines != nullptr && m_backupAsmCode.size() != 0) {
			LPVOID patchAddress = (LPVOID)((size_t)m_sqlite3Rountines->backup_init + 0xAD);
			WriteProcessMemory(GetCurrentProcess(), patchAddress, (LPCVOID)m_backupAsmCode.data(), m_backupAsmCode.size(), 0);
			m_backupAsmCode.resize(0);
		}
	}

	nlohmann::ordered_json DatabaseMgr::execute(sqlite3* db, const std::string& sql)
	{
		nlohmann::ordered_json rdata = { {"status",0},{"desc",""} };
		if (db == nullptr) {
			rdata["status"] = -1;
			rdata["desc"] = "input db handle is nullptr";
			return rdata;
		}
		sqlite3_stmt* stmt = nullptr;
		int rc = m_sqlite3Rountines->prepare((LPVOID)db, sql.c_str(), -1, &stmt, 0);
		if (rc != SQLITE_OK) {
			rdata["status"] = rc;
			rdata["desc"] = format_string("execute %s failed", "sqlite3_prepare");
			return rdata;
		}
		nlohmann::ordered_json items = nlohmann::ordered_json::array();
		while (m_sqlite3Rountines->step(stmt) == SQLITE_ROW)
		{
			int col_count = m_sqlite3Rountines->column_count(stmt);
			nlohmann::ordered_json item;
			for (int i = 0; i < col_count; i++)
			{
				const char* ColName = m_sqlite3Rountines->column_name(stmt, i);
				int nType = m_sqlite3Rountines->column_type(stmt, i);
				const void* pReadBlobData = m_sqlite3Rountines->column_blob(stmt, i);
				int nLength = m_sqlite3Rountines->column_bytes(stmt, i);
				std::string key(ColName);
				std::string value;
				switch (nType)
				{
				case SQLITE_BLOB:
				{
					value = toHexString(std::string((char*)pReadBlobData, nLength));
					break;
				}
				default:
				{
					value = std::string((char*)pReadBlobData, nLength);
					break;
				}
				}
				item[key] = value;
			}
			items.push_back(item);
		}
		m_sqlite3Rountines->finalize(stmt);
		rdata["data"] = items;
		return rdata;
	}

	nlohmann::ordered_json DatabaseMgr::execute(const std::string& dbname, const std::string& sql)
	{
		nlohmann::ordered_json rdata = { {"status",0},{"desc",""} };
		LPVOID dbHandle = getDatabaseHandle(dbname);
		if (dbHandle == nullptr) {
			rdata["status"] = -1;
			rdata["desc"] = format_string("get database handle which named %s failed", dbname);
			return rdata;
		}
		rdata = execute((sqlite3*)dbHandle, sql);
		return rdata;
	}

	std::string DatabaseMgr::codec_get_key(const std::string& dbname) {
		LPVOID dbHandle = getDatabaseHandle(dbname);
		if (dbHandle == nullptr)
			return "";
		return codec_get_key((sqlite3*)dbHandle);
	}

	std::string DatabaseMgr::codec_get_key(sqlite3* db) {
		char* pKey = nullptr;
		int iLen = 0;
		std::string szKey;
		return szKey;
		if (m_codecGetKey == nullptr)
			return szKey;
		nlohmann::ordered_json queryResult = execute(db, std::string("PRAGMA cipher_store_pass"));
		if (queryResult["data"][0]["cipher_store_pass"] == 0) {
			return szKey;
		}
		::codec_get_key(m_codecGetKey, db, 0, (void**)&pKey, &iLen);
		if (pKey == nullptr)
			return szKey;
		szKey = std::string(pKey, iLen);
		return szKey;
	}

	void DatabaseMgr::backup_xprogress(int remaining, int pagecount) {
		//LL_DEBUG("backup process: %d/%d\n", pagecount - remaining, pagecount);
		//OutputDebugStringA("[backup process]\n");
	}

	int DatabaseMgr::backup(sqlite3* db, const std::string& out_path)
	{
		int rc = SQLITE_OK;
		if (db == nullptr) {
			return -1;
		}
		sqlite3* pNewDbHandle = nullptr;
		sqlite3_backup* pBackupHandle = nullptr;
		rc = m_sqlite3Rountines->open(out_path.c_str(), &pNewDbHandle);
		if (rc == SQLITE_OK) {
			pBackupHandle = m_sqlite3Rountines->backup_init(pNewDbHandle, (const char*)"main", db, (const char*)"main");
			if (pBackupHandle) {
				do {
					rc = m_sqlite3Rountines->backup_step(pBackupHandle, 5);
					backup_xprogress(
						m_sqlite3Rountines->backup_remaining(pBackupHandle),
						m_sqlite3Rountines->backup_pagecount(pBackupHandle)
					);
					if (rc == SQLITE_OK || rc == SQLITE_BUSY || rc == SQLITE_LOCKED) {
						m_sqlite3Rountines->sleep(50);
					}
				} while (rc == SQLITE_OK || rc == SQLITE_BUSY || rc == SQLITE_LOCKED);
				(void)m_sqlite3Rountines->backup_finish(pBackupHandle);
			}
			rc = m_sqlite3Rountines->errcode(pNewDbHandle);
		}
		(void)m_sqlite3Rountines->close(pNewDbHandle);
		return rc;
	}

	int DatabaseMgr::backup(const std::string& dbname, const std::string& out_path)
	{
		int rc = SQLITE_OK;
		sqlite3* pExistDbHandle = (sqlite3*)getDatabaseHandle(dbname);
		if (pExistDbHandle == nullptr) {
			return -1;
		}
		return backup(pExistDbHandle, out_path);
	}

	const std::map<std::string, LPVOID>& DatabaseMgr::searchDatabases() {
		std::lock_guard<std::mutex> lg(m_searchDbMtx);
		if (!HandleScanAllowed()) {
			m_dbs.clear();
			return m_dbs;
		}
		for (auto& it : m_dbs) {
			if (IsBadReadPtr(it.second, sizeof(LPVOID)) || m_sqlite3Rountines->errcode(it.second) == SQLITE_MISUSE) {
				m_dbs.clear();
				break;
			}
		}
		if (m_dbs.size() > 0) {
			return m_dbs;
		}
		std::vector<LPVOID> results;
		size_t vfs_addr = (size_t)g_hWeixinDll + XWECHAT_SQLITE3_VFS_OFFSET;
		ScanPattern(GetCurrentProcess(), (BYTE*)&vfs_addr, sizeof(LPVOID), results);
		for (size_t i = 0; i < results.size(); i++) {
			auto result = results[i];
			if (IsBadReadPtr(result, sizeof(LPVOID)) == 0) {
				int rc = m_sqlite3Rountines->errcode(result);
				if (rc == SQLITE_OK) {
					nlohmann::ordered_json queryResult = execute(result, "PRAGMA database_list");
					std::string dbpath = queryResult["data"][0]["file"].get<std::string>();
					if (dbpath.empty())
						continue;
					auto pos = dbpath.find_last_of("\\");
					std::string dbname = dbpath.substr(pos + 1);
					m_dbs[dbname] = result;
				}
			}
		}
		return m_dbs;
	}

	// 句柄表存活校验（看门狗每次只调这一个函数）：
	//   * 只读缓存，不 clear、不扫描（m_dbs 的写侧仍在 searchDatabases 的锁里）；
	//   * 判据只有一条「有没有一个句柄的地址能读」——**不碰 sqlite 内部状态**，
	//     所以即便微信正在重建句柄表，这里也不会替它推进任何状态、不会制造新的崩溃面。
	// 返回 false 的语义：缓存里一个能读的句柄都没有 → 上层把门禁关掉、等下次就绪。
	bool DatabaseMgr::HandleTableAlive()
	{
		std::lock_guard<std::mutex> lg(m_searchDbMtx);
		if (m_dbs.empty())
			return false;
		for (auto& it : m_dbs) {
			if (HandleLooksAlive(it.second))
				return true;
		}
		return false;
	}

	// 公开入口（全局命名空间，见 global.h）：句柄表存活 + 只读快照。
	// ⚠️ 这两个**不能**放在 namespace xmgr 里：global.h 是按全局符号声明的，
	// 放进来会变成 xmgr:: 修饰名，链接期找不到（实测 LNK2019）。

	void DatabaseMgr::HandlesSnapshot(std::vector<std::pair<std::string, uint64_t>>& out)
	{
		std::lock_guard<std::mutex> lg(m_searchDbMtx);
		out.clear();
		out.reserve(m_dbs.size());
		for (auto& it : m_dbs)
			out.push_back({ it.first, (uint64_t)(uintptr_t)it.second });
	}

	// 清缓存 + 真扫一遍。调用方（就绪判据）自己保证不在查询门禁开着时调它。
	void DatabaseMgr::ForceRescan()
	{
		{
			std::lock_guard<std::mutex> lg(m_searchDbMtx);
			m_dbs.clear();
		}
		searchDatabases();          // 缓存空 → 会走全内存扫描
	}

	// 公开入口：库名 -> 句柄数值快照（只读缓存，不重建）
	void DbHandlesSnapshot(std::vector<std::pair<std::string, uint64_t>>& out)
	{
		DatabaseMgr::getInstance().HandlesSnapshot(out);
	}

	LPVOID DatabaseMgr::getDatabaseHandle(const std::string& dbname)
	{
		auto& dbs = searchDatabases();
		if (m_dbs.find(dbname) == m_dbs.end())
			return nullptr;
		return m_dbs[dbname];
	}

	nlohmann::ordered_json DatabaseMgr::getDatabaseInfo() {
		nlohmann::ordered_json jdbs = nlohmann::ordered_json::array();
		if (!DbReady())
		{
			//OutputDebugStringA("[getDatabaseInfo] 用户未登录\n");
			return jdbs;
		}
			// do force research

		//OutputDebugStringA("[getDatabaseInfo] 用户已登录\n");

		m_dbs.clear();
		auto& dbs = searchDatabases();
		for (auto& db : dbs) {
			nlohmann::json jdb = { {"dbName",db.first},{"dbHandle",(size_t)db.second} };
			
			//std::string debugMsg = "[getDatabaseInfo] dbName " + db.first + "\n";
			//OutputDebugStringA(debugMsg.c_str());

			// jdb["dbKey"] = codec_get_key(db.second);
			jdbs.push_back(jdb);
		}
		return jdbs;
	}
}

// ---- 给就绪判据/看门狗用的三个全局入口（声明在 global.h）----
bool DbHandleTableAlive()
{
	return xmgr::DatabaseMgr::getInstance().HandleTableAlive();
}

void DbHandlesSnapshot(std::vector<std::pair<std::string, uint64_t>>& out)
{
	xmgr::DatabaseMgr::getInstance().HandlesSnapshot(out);
}

// 强制重扫：先清缓存，再让 searchDatabases 真的去扫一遍进程内存。
// 为什么需要它：就绪判据要拿「当前真实句柄表」判断 db 层能不能用；只读缓存会拿到上一次的结论。
// **这是重活**（全内存扫描），只在闸门冷路径调用。
void DbForceRescan()
{
	xmgr::DatabaseMgr::getInstance().ForceRescan();
}
