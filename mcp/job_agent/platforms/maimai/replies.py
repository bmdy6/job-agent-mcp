"""No verified recruitment message entry exists in the inspected public website."""
class MaimaiReplySource:
    platform='maimai'

    def fetch(self,space,*,since=None,cursor=None):
        raise NotImplementedError('脉脉网页招聘消息入口未核验；不打开社区私信或添加好友')

ReplySource=MaimaiReplySource
