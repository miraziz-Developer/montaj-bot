// All user-facing strings (Uzbek, Latin script).

export const t = {
  loading: "Yuklanmoqda…",
  openViaBot: "Iltimos, botdagi tugma orqali oching.",
  balance: (n) => `Balans: ${n} birlik`,
  trialBadge: "🎁 1 ta bepul sinov (1 daqiqagacha)",
  pickVideo: "🎬 Video tanlash",
  cancel: "Bekor qilish",
  uploading: "Video yuklanmoqda",
  speed: (mbps) => `${mbps} MB/s`,
  remaining: (mmss) => `Qoldi: ${mmss}`,
  verifying: "Video tekshirilmoqda…",
  duration: "Davomiyligi",
  resolution: "O‘lchami",
  price: (n) => `Narxi: ${n} birlik`,
  balanceAfter: (n) => `Balansdan keyin: ${n} birlik`,
  trialFree: "🎁 Bepul sinov",
  formatTitle: "Format",
  styleTitle: "Uslub",
  briefLabel: "Qanday montaj xohlaysiz? (ixtiyoriy)",
  start: "Boshlash",
  done: "✅ Qabul qilindi! Natija haqida bot sizga yozadi.",
  retry: "Qayta urinish",
  close: "Yopish",
  tariffs: "💳 Tariflar",
  footer: "Fayllar 48 soatdan keyin avtomatik o‘chiriladi.",
  genericError: "Xatolik yuz berdi. Iltimos, qayta urinib ko‘ring.",
  networkError: "Internet aloqasi yo‘q. Yuklash to‘xtatildi, qayta urinib ko‘ring.",
  unsupportedFile: "Bu fayl turi qo‘llab-quvvatlanmaydi.",
  emptyFile: "Fayl bo‘sh.",
  insufficient: "Birliklar yetarli emas.",
  brollTitle: "B-roll qo‘shish (ixtiyoriy)",
  brollHint:
    "Diktor gapirayotganda ko‘rsatiladigan qo‘shimcha kadrlar (masalan, mahsulotning boshqa burchagi). " +
    "AI mos joyda ishlatadi. Eng ko‘pi 4 ta, qo‘shilgandan keyin olib tashlab bo‘lmaydi.",
  brollAdd: "➕ Video qo‘shish",
  brollContinue: "Davom etish",
  brollUploading: (pct) => `Yuklanmoqda… ${pct}%`,
  brollItem: (n, sec) => `B-roll ${n} — ${sec}`,
  brollCapReached: "Eng ko‘pi 4 ta B-roll qo‘shish mumkin.",
};

export const FORMATS = [
  { value: "9:16", label: "9:16 Reels/TikTok" },
  { value: "16:9", label: "16:9 YouTube" },
  { value: "1:1", label: "1:1 Kvadrat" },
  { value: "original", label: "Original" },
];

export const STYLES = [
  { value: "dynamic_reels", label: "Dinamik Reels", hint: "tez kesishlar, yorqin subtitr" },
  { value: "clean_talk", label: "Sokin gap", hint: "kam effekt, toza subtitr" },
  { value: "ad_commercial", label: "Reklama", hint: "kuchli boshlanish, chaqiriq matni" },
  { value: "vlog_story", label: "Vlog/hikoya", hint: "ketma-ketlik saqlanadi" },
];

export function errorText(error) {
  if (error?.code === "NETWORK") return t.networkError;
  return error?.messageUz || t.genericError;
}
