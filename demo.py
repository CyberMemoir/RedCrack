from request.web.xhs_session import create_xhs_session
from loguru import logger
import asyncio

async def guest_demo():
	xhs_session = await create_xhs_session(proxy="http://127.0.0.1:7890")
	res = await xhs_session.apis.note.get_homefeed(xhs_session.apis.note.homefeed_category_enum.FOOD)
	logger.success("笔记详情 | " + (await res.text()))
	await xhs_session.close_session()

async def login_demo():
	xhs_session = await create_xhs_session(proxy="http://127.0.0.1:7890", web_session="030037afxxxxxxxxxxxxxxxxxxxaeb59d5b4")
	res = await xhs_session.apis.auth.get_self_simple_info()

	logger.success("账号信息 | " + (await res.text()))
	await xhs_session.close_session()


# 需自行完善app协议 方可使用
# async def app_scan_login_demo():
# 	xhs_session = await create_xhs_session(proxy="http://127.0.0.1:7890", sid="179999999999999999999")
# 	res = await xhs_session.apis.auth.get_self_simple_info()
# 	logger.success("账号信息 | " + (await res.text()))
# 	await xhs_session.close_session()


async def main():
	await guest_demo()
	await login_demo()
	# await app_scan_login_demo()

if __name__ == "__main__":
	asyncio.run(main())
