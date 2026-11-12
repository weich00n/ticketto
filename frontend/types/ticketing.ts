export type EventStatus =
  | "On sale"
  | "High demand"
  | "Upcoming sale"
  | "Almost sold out"
  | "Sold out"
  | "Event updated"
  | "Event cancelled"
  | "Event ended"
  | "Draft";
export type TicketCategory = {
  id: string;
  name: string;
  price: number;
  available: number;
  benefits: string;
};
export type Event = {
  id: string;
  name: string;
  subtitle: string;
  category: string;
  date: string;
  time: string;
  venue: string;
  location: string;
  image: string;
  status: EventStatus;
  price: number;
  description: string;
  organizer: string;
  categories: TicketCategory[];
  release: string;
  maxTickets: number;
  waitingRoom: boolean;
};
export type Reservation = {
  eventId: string;
  categoryId?: string;
  // Set once the Reservation Service holds the tickets.
  holdId?: string;
  category: string;
  quantity: number;
  price: number;
  seats: string[];
  expires: number;
};
export type Order = {
  id: string;
  reservation: Reservation;
  buyer: string;
  email: string;
  total: number;
  date: string;
};
export type DigitalTicket = {
  id: string;
  eventId: string;
  buyer: string;
  category: string;
  seat: string;
  status: "Valid" | "Past" | "Cancelled";
  // QR code image from the Ticket Service; demo tickets draw a placeholder.
  qrImage?: string;
};
