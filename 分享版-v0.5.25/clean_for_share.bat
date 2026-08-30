@echo off
chcp 65001 >nul
title 发布前清理（分享给朋友 / 上传 GitHub 前运行）
cd /d "%~dp0"

echo ============================================
echo   发布前清理 - 删除账号敏感数据
echo ============================================
echo.
echo 本脚本会删除以下内容（全部是隐私/凭证数据）：
echo   data\                    账号登录态、好友名单、消息模板、运行历史、日志
echo   .env                     网页访问令牌
echo.
echo 警告：删除后你的抖音账号登录态会丢失，需重新扫码！
echo 请先关闭 start.bat 黑窗口（停止服务器）再继续。
echo.
pause

echo 正在清理...
rmdir /s /q data 2>nul
del /q .env 2>nul

echo.
echo ✅ 清理完成！
echo.
echo 现在这个文件夹可以放心复制/压缩/上传到 GitHub 了。
echo 新用户第一次双击 start.bat 会自动生成干净的 .env 和默认账号，
echo 之后用「网页内扫码登录」关联自己的抖音账号即可。
echo.
pause
