from telethon.sync import TelegramClient, events
import asyncio
import google.generativeai as genai
import random
import sqlite3
import os
import uuid
from telethon.sync import TelegramClient
from telethon import functions
from dotenv import load_dotenv

# Configure Gemini
load_dotenv()
genai.configure(api_key=os.getenv("api_key"))
# Your API credentials
api_id = os.getenv("api_id")
api_hash = os.getenv("api_hash")
phone = os.getenv("phone")
processed_albums = set()
SOURCE_CHANNELS = []
# Dynamically get channels from folder "новости"
with TelegramClient('get_channels', api_id, api_hash) as client:
    req = client(functions.messages.GetDialogFiltersRequest())
print("Channels ids:")
for channel in req.to_dict()["filters"][1]['include_peers']:
    SOURCE_CHANNELS.append(channel['channel_id'])
    print(channel['channel_id'])
REVIEWER_ID = 536196537  # Your personal account ID
REVIEW_CHANNEL = 'https://t.me/+d0YweaWtS8wxYTli'
TARGET_CHANNEL = 'FlazyNews'

# Database setup
def init_db():
    conn = sqlite3.connect('posts.db')
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS posts
                 (id TEXT PRIMARY KEY, text TEXT, media_path TEXT, 
                 status TEXT, review_msg_id INTEGER)''')
    conn.commit()
    return conn

db_conn = init_db()
pending_posts = {}

# Gemini functions
def rewrite_game_news(original_text):
    prompt = f"""
        Transform this game news into Russian following these EXACT requirements:

        1. CONTENT RULES:
        - Preserve ALL original information 100%
        - Never add new facts/features
        - Remove ALL external links
        - Keep text under 1024 characters

        2. STRUCTURE FORMAT (all emojis are examples):
        ✨ [Game Name] - [Catchy Headline in Caps]

        ⚡️ [Key Feature/Update]

        🎮 [Important Detail 1]

        💰 [Important Detail 2] (if there is enough information)

        💰 [Important Detail 3] (if there is enough information)

        📅 Event Dates: [Start] - [End] (if mentioned)

        3. STYLE REQUIREMENTS:
        - Use 3-5 relevant emojis MAX (1 per paragraph in the beginning) 
        - Add 1-3 hashtags at the end from these categories:
        #GameTitle #FeatureName #EventType
        - Maintain professional news tone
        - Separate paragraphs with blank lines
        - If paragrahs are too long, split them into more paragraphs
        - Include original dates if present

        4. PROHIBITED:
        - External links
        - Made-up information
        - Overuse of emojis (>5)
        - Informal language
        - Comments/meta text

        Example Output Format:
        🚨 PUBG MOBILE - ОБНОВЛЕНИЕ СЕЗОНА 10!

        ⚡️ Новая карта Арена

        🎮 5 уникальных режимов

        💰 Бонусы за ранний вход

        📅 Доступно с 15.08 по 20.09
        #PUBGMOBILE #НовыйСезон #БатлРояль

        Original Text: {original_text}
        """
    
    response = genai.GenerativeModel('models/gemma-3-27b-it').generate_content(
        prompt,
        generation_config={"temperature": 0.4, "top_p": 0.9}
    )
    return response.text

async def safe_gemini_request(text):
    try:
        return rewrite_game_news(text)
    except Exception as e:
        if "quota" in str(e).lower():
            wait = random.uniform(10, 30)
            await asyncio.sleep(wait)
            return await safe_gemini_request(text)
        raise

async def safe_send_media(client, chat_id, text, media_path):
    if not media_path or len(text) <= 1024:
        return await client.send_file(
            chat_id,
            media_path,
            caption=text[:1024] if media_path else None
        )
    
    msg = await client.send_file(chat_id, media_path)
    await client.send_message(chat_id, text, reply_to=msg.id)

async def send_for_approval(client, event, text, media, is_first_message=True):
    post_id = str(uuid.uuid4())
    media_paths = []

    try:
        # Save media paths
        if isinstance(media, list):  # Multiple media (album)
            for path in media:
                new_path = f"media/{post_id}_{os.path.basename(path)}"
                os.rename(path, new_path)
                media_paths.append(new_path)
        elif isinstance(media, str):  # Single file
            new_path = f"media/{post_id}{os.path.splitext(media)[1]}"
            os.rename(media, new_path)
            media_paths = [new_path]

        # Save post
        pending_posts[post_id] = {
            'text': text,
            'media_path': media_paths,
            'status': 'pending',
            'messages': []
        }

        #reviewer = await client.get_input_entity(REVIEWER_ID)
        reviewer = await client.get_entity(REVIEW_CHANNEL)
        messages = []
        if is_first_message:
            media_msg = await event.forward_to(reviewer)
            messages.append(media_msg)

        text_msg = await client.send_message(
            reviewer,
            f"📝 Post Review Needed (ID: {post_id})\n\n{text[:4000]}"
        )
        messages.append(text_msg.id)

        cmd_msg = await client.send_message(
            reviewer,
            f"🛠 Commands for post {post_id}:\n"
            f"`/approve_{post_id}`\n"
            f"`/reject_{post_id}`\n"
            f"`/edit_{post_id} <new_text>`",
            reply_to=text_msg.id
        )
        messages.append(cmd_msg.id)

        c = db_conn.cursor()
        c.execute("INSERT INTO posts VALUES (?,?,?,?,?)",
                 (post_id, text, ';'.join(media_paths), 'pending', ','.join(map(str, messages))))
        db_conn.commit()

    except Exception as e:
        print(f"Failed to send approval request: {e}")
        for path in media_paths:
            if os.path.exists(path):
                os.remove(path)
        if post_id in pending_posts:
            del pending_posts[post_id]

async def approve_post(client, post_id):
    try:
        post = pending_posts[post_id]
        media_paths = post['media_path'] if isinstance(post['media_path'], list) else [post['media_path']]

        if media_paths and all(os.path.exists(p) for p in media_paths):
            await client.send_file(TARGET_CHANNEL, media_paths, caption=post['text'][:1024])
        else:
            await client.send_message(TARGET_CHANNEL, post['text'])

        pending_posts[post_id]['status'] = 'approved'
        c = db_conn.cursor()
        c.execute("UPDATE posts SET status=? WHERE id=?", ('approved', post_id))
        db_conn.commit()

        for path in media_paths:
            if os.path.exists(path):
                os.remove(path)

    except Exception as e:
        print(f"Failed to approve post {post_id}: {e}")


async def handle_approval_command(client, event):
    try:
        _, post_id = event.text.split('_', 1)
        if post_id in pending_posts:
            if pending_posts[post_id]['status'] == 'pending':
                await approve_post(client, post_id)
                await event.reply(f"✅ Post {post_id} approved and published!")
            else:
                await event.reply(f"❌ Post {post_id} has already been processed.")
        else:
            await event.reply("❌ Invalid post ID")
    except Exception as e:
        await event.reply(f"❌ Error: {str(e)}")

async def handle_reject_command(client, event):
    try:
        _, post_id = event.text.split('_', 1)
        if post_id in pending_posts:
            post = pending_posts[post_id]
            media_paths = post['media_path'] if isinstance(post['media_path'], list) else [post['media_path']]
            pending_posts[post_id]['status'] = 'rejected'
            c = db_conn.cursor()
            c.execute("UPDATE posts SET status=? WHERE id=?", ('rejected', post_id))
            db_conn.commit()
            
            for path in media_paths:
                if os.path.exists(path):
                    os.remove(path)
            
            await event.reply(f"❌ Post {post_id} rejected and deleted!")
        elif post_id == 'all':
            for post_id, post in list(pending_posts.items()):
                if post['status'] == 'pending':
                    media_paths = post['media_path'] if isinstance(post['media_path'], list) else [post['media_path']]
                    pending_posts[post_id]['status'] = 'rejected'
                    c = db_conn.cursor()
                    c.execute("UPDATE posts SET status=? WHERE id=?", ('rejected', post_id))
                    db_conn.commit()
                    
                    for path in media_paths:
                        if os.path.exists(path):
                            os.remove(path)
            
            await event.reply("❌ All pending posts rejected and deleted!")
        else:
            await event.reply("❌ Invalid post ID")
    except Exception as e:
        await event.reply(f"❌ Error: {str(e)}")

async def handle_edit_command(client, event):
    try:
        # Parse command: /edit_<post_id> <new_text>
        parts = event.text.split(' ', 1)
        if len(parts) < 2:
            await event.reply("❌ Usage: /edit_<post_id> <new_text>")
            return
        _, post_id = parts[0].split('_', 1)
        if post_id not in pending_posts:
            await event.reply("❌ Invalid post ID")
            return
        new_text = parts[1]
        pending_posts[post_id]['text'] = new_text
        c = db_conn.cursor()
        c.execute("UPDATE posts SET text=? WHERE id=?", (new_text, post_id))
        db_conn.commit()
        await send_for_approval(client, event, new_text, pending_posts[post_id]['media_path'], is_first_message=False) #TODO dont forward media again
    except Exception as e:
        await event.reply(f"❌ Error: {str(e)}")

async def main():
    client = TelegramClient('parser', api_id, api_hash)

    await client.start(phone)
    
    if not await client.is_user_authorized():
        await client.send_code_request(phone)
        await client.sign_in(phone, input('Enter the code: '))
    @client.on(events.Album(chats=SOURCE_CHANNELS))
    async def album_handler(event):
        try:
            if event.grouped_id in processed_albums:
                return
            processed_albums.add(event.grouped_id)

            print(f"Processing album from {event.chat.title}")
            # Collect all media files in album
            media_paths = []
            for msg in event.messages:
                if msg.media:
                    path = await msg.download_media(file=f"media/{msg.id}")
                    media_paths.append(path)

            # Choose caption from the first message with text
            caption_msg = next((m for m in event.messages if m.message), None)
            original_text = caption_msg.message if caption_msg else ''

            rewritten = await safe_gemini_request(original_text)
            await send_for_approval(client, event, rewritten, media_paths)

        except Exception as e:
            print(f"Error processing album from {event.chat.title}: {e}")

    @client.on(events.NewMessage(chats=SOURCE_CHANNELS))
    async def channel_handler(event):
        if event.grouped_id: return
        print("Processing new message...")
        try:
            rewritten = await safe_gemini_request(event.message.text)
            media_path = None
            
            if event.message.media:
                media_path = await event.download_media(file=f"media/{event.id}")
            await send_for_approval(client, event, rewritten, media_path)
            
        except Exception as e:
            print(f"Error processing {event.chat.title}: {e}")

    @client.on(events.NewMessage(chats=REVIEW_CHANNEL))
    async def approval_handler(event):
        try:
            if event.text.startswith('/approve_'):
                print("Handling approval command...")
                await handle_approval_command(client, event)
            elif event.text.startswith('/reject_'):
                print("Handling reject command...")
                await handle_reject_command(client, event)
            elif event.text.startswith('/edit_'):
                print("Handling edit command...")
                await handle_edit_command(client, event)
            elif event.text.startswith('/pending_posts'):
                print("Handling pending posts command...")
                pending_posts_list = "\n".join([f"`{post_id}`: {post['text'][:50]}..." for post_id, post in pending_posts.items() if post['status'] == 'pending']) #TODO: update on server
                await event.reply(f"Pending posts:\n{pending_posts_list}")
        except Exception as e:
            print(f"Error handling approval command: {e}")

    print("Monitoring channels and awaiting approvals...")
    await client.run_until_disconnected()

if __name__ == '__main__':
    os.makedirs('media', exist_ok=True)
    asyncio.run(main())


#TODO: check if database needed,