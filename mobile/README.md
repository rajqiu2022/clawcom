# ClawTeam 移动端

Android WebView 与 HarmonyOS ArkWeb 共用 Hub 的 `/mobile` 界面及同源 `/api/v1` 接口。移动 H5 站点经太湖网关代理时，服务端用 `TAI_MOBILE_GATEWAY_TOKEN` 校验签名头并建立 Hub session。未接入移动站点的入口仍走 Hub 已有的 NGN/WOA SSO。移动网关的手机 iOA 鉴权以**从手机 iOA 访问已配置的移动 H5 站点**为前提；独立安装的 WebView/ArkWeb 壳是否能完成 iOA 跳转，需用目标设备验证，不能仅凭网页壳推定。

## 功能

- Agent 团队：项目和团队切换、成员、频道聊天及 `@全部 Agent`。
- 团队任务：测试计划任务与工作流任务/阶段查看。
- Hub 报告：最近七天报告及内容。
- 知识库：项目知识搜索、详情、新建、收藏。
- Claw 管理：项目实例、状态、职责、名称与职位编辑；完整设置链接回 Hub 页面。

团队频道依赖服务端 `CHAT_ROOM_ENABLED=1`，Agent 团队依赖服务端对应项目已启用。所有写操作沿用 Hub API 的权限检查。

## 太湖移动站点接入

1. 在太湖创建“移动 H5 站点”，勾选“手机 iOA 员工鉴权”，将站点回源到 Hub，使 `/mobile`、`/api/v1/*` 和 `/static/*` 同域可访问。
2. 在 Hub 服务端配置此移动站点的应用 token 为 `TAI_MOBILE_GATEWAY_TOKEN`。服务端校验 `TIMESTAMP`、`SIGNATURE`、`STAFFID`、`STAFFNAME`、`X-RIO-SEQ`、`X-EXT-DATA`，校验签名并限制时间偏差 180 秒；不要把 token 放进 H5 或客户端工程。
3. 用手机 iOA 打开移动站点域名的 `/mobile`，验证登录、API 会话和用户项目权限。若双端壳仍有独立应用要求，再在各自工程中将入口改为此域名并验收回调。

依据：[太湖移动站点鉴权原理](https://iwiki.woa.com/p/1773289334)和[移动站点签名校验](https://iwiki.woa.com/pages/viewpage.action?pageId=1784517419)。

## 构建

Android：用 Android Studio 打开 `mobile/android` 并执行 `:app:assembleDebug`。需 Android SDK 36、Gradle 8.14 和 AGP 8.11.1。调试包在 `android/app/build/outputs/apk/debug/app-debug.apk`。本机离线构建如缺少 Maven aapt2，可向 Gradle 传 `-Pandroid.aapt2FromMavenOverride=<Android SDK>/build-tools/35.0.0/aapt2.exe`。

HarmonyOS：用 DevEco Studio 打开 `mobile/harmony`，配置应用签名后执行 `assembleHap`。未签名的编译产物在 `harmony/entry/build/default/outputs/default/entry-default-unsigned.hap`，不能直接作为正式安装包发布。

两个原生工程的服务地址均为 `https://clawteam.woa.com/mobile`。如需测试其他 Hub 环境，分别修改 Android `MainActivity.java` 的 `HUB_URL` 与 Harmony `Index.ets` 的 Web `src`。正式打包时应固定已完成 HTTPS 与 SSO 配置的 Hub 域名。
