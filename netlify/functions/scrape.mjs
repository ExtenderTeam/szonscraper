// Ütemezett függvény: 15 percenként frissíti a híreket.
// Figyelem: a Netlify csak a publikált (production) deployon futtatja.
import { scrape } from "../lib/scraper.mjs";

export default async () => {
  const res = await scrape();
  console.log("Scrape kész:", JSON.stringify(res));
};

export const config = { schedule: "*/15 * * * *" };
