import random

class AijaMattoCommand:

	def execute(self, args=None):
		matto = random.choice(list(open('aijamatto.txt', encoding='UTF-8')))
		return matto