#pragma once

struct CALL_CONTEXT;

namespace hook {

	void MyCallHandler_xLog(::CALL_CONTEXT* ctx);

	// xlog 明文捕获。**默认关**：只有 %TEMP%\wx_xlog_capture.on 存在才安装。
	// 为什么要它：盘上的 .xlog 是 RSA 加密的（mars MAGIC_COMPRESS_START2），
	// 唯一能拿到明文的地方就是写入路径、加密之前。详见 src/hook_xlog.cpp 顶部。
	bool InstallXlogCapture();

}
