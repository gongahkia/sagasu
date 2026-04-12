const isDev = import.meta.env.DEV;
export const ROOM_DATA_URL = isDev
  ? '/data/scraped_log.json'
  : 'https://raw.githubusercontent.com/gongahkia/sagasu-2/main/backend/log/scraped_log.json';
export const BOOKINGS_URL = isDev
  ? '/data/scraped_bookings.json'
  : 'https://raw.githubusercontent.com/gongahkia/sagasu-2/main/backend/log/scraped_bookings.json';
export const TASKS_URL = isDev
  ? '/data/scraped_tasks.json'
  : 'https://raw.githubusercontent.com/gongahkia/sagasu-2/main/backend/log/scraped_tasks.json';
export const SCRAPER_CONSOLE_URL = isDev
  ? '/data/scraper_console.txt'
  : 'https://raw.githubusercontent.com/gongahkia/sagasu-2/main/backend/log/scraper_console.txt';
